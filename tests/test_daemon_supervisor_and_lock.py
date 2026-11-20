"""Tests for HyperText v0.6.4 Self-Healing Daemon Supervisor, `flock` Single-Instance Lock, `AGENTS.md`, and `codex_review.sh`."""

from __future__ import annotations

import asyncio
import contextlib
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from hypertext import (
    AgentConfig,
    InboundMessage,
    MacIMessageBridge,
    acquire_daemon_lock,
    main as cli_main,
)
from dataclasses import asdict

from hypertext.bridge_imessage import APPLE_EPOCH_OFFSET, MessageDeliveryError
from hypertext.cli import run_imessage_daemon
from hypertext.daemon_control import DaemonLog, MAX_LOG_BYTES, STOP_REQUEST
from hypertext.human_gate import HumanGate
from hypertext.orchestrator import HyperTextOrchestrator
from hypertext.store_sqlite import StateStore


def _init_minimal_chat_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT, chat_identifier TEXT);
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY,
            guid TEXT,
            text TEXT,
            attributedBody BLOB,
            handle_id INTEGER,
            is_from_me INTEGER DEFAULT 0,
            date INTEGER DEFAULT 0,
            cache_has_attachments INTEGER DEFAULT 0,
            associated_message_type INTEGER DEFAULT 0
        );
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
        """
    )
    conn.commit()
    conn.close()


class TestDaemonSupervisorAndLockV063(unittest.IsolatedAsyncioTestCase):
    def test_daemon_lock_single_instance_fd_cloexec_and_pid_protection(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            chat_db = tmp_path / "chat.db"
            _init_minimal_chat_db(chat_db)

            with acquire_daemon_lock(state_dir) as fd1:
                self.assertIsNotNone(fd1)
                assert fd1 is not None
                # Must be non-inheritable (FD_CLOEXEC) so child CLIs/Chromium never hold daemon.lock
                self.assertFalse(os.get_inheritable(fd1))
                pid_file = state_dir / "daemon.pid"
                self.assertTrue(pid_file.is_file())
                self.assertEqual(pid_file.read_text(encoding="utf-8").strip(), str(os.getpid()))

                # Duplicate in-process attempt yields None without overwriting daemon.pid
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    with acquire_daemon_lock(state_dir) as fd2:
                        self.assertIsNone(fd2)
                self.assertIn("already running", buf.getvalue())
                self.assertEqual(pid_file.read_text(encoding="utf-8").strip(), str(os.getpid()))

                # Duplicate child process attempt also sees the lock (proving FD was NOT inherited as unlocked
                # and kernel flock blocks separate processes cleanly with exit code 0)
                env_file = tmp_path / ".env"
                env_file.write_text(
                    f"DEEPSEEK_API_KEY=sk-valid-lock-key\n"
                    f"HYPERTEXT_STATE_DIR={state_dir}\n"
                    f"CHAT_DB_PATH={chat_db}\n",
                    encoding="utf-8",
                )
                dup_proc = subprocess.run(
                    [sys.executable, str(repo_root / "hypertext_agent.py"), "--env-file", str(env_file)],
                    cwd=str(repo_root),
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(dup_proc.returncode, 0)
                self.assertIn("already running", dup_proc.stdout + dup_proc.stderr)
                self.assertEqual(pid_file.read_text(encoding="utf-8").strip(), str(os.getpid()))

            # After context exit, daemon.pid is unlinked and a new lock succeeds immediately
            self.assertFalse((state_dir / "daemon.pid").exists())
            with acquire_daemon_lock(state_dir) as fd3:
                self.assertIsNotNone(fd3)

    def test_non_daemon_cli_commands_work_while_daemon_lock_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            chat_db = tmp_path / "chat.db"
            _init_minimal_chat_db(chat_db)
            env_file = tmp_path / ".env"
            env_file.write_text(
                f"DEEPSEEK_API_KEY=sk-valid-coexist-key\n"
                f"ALLOWED_SENDERS=+8613800138000\n"
                f"ENABLE_INVITE_CODES=true\n"
                f"HYPERTEXT_STATE_DIR={state_dir}\n"
                f"CHAT_DB_PATH={chat_db}\n",
                encoding="utf-8",
            )

            with acquire_daemon_lock(state_dir) as fd:
                self.assertIsNotNone(fd)
                for cli_args in (
                    ["--env-file", str(env_file), "--doctor"],
                    ["--env-file", str(env_file), "--create-invite", "--invite-code", "HT-LOCKOK"],
                    ["--env-file", str(env_file), "--list-users"],
                    ["--env-file", str(env_file), "--allow-user", "+8613900007777"],
                    ["--env-file", str(env_file), "--revoke-user", "+8613900007777"],
                ):
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        rc = cli_main(cli_args)
                    self.assertEqual(rc, 0, f"Expected rc=0 for {cli_args}, got {rc}")
                # daemon.pid of the active holder must remain untouched
                self.assertEqual(
                    (state_dir / "daemon.pid").read_text(encoding="utf-8").strip(),
                    str(os.getpid()),
                )

    async def test_sighup_immunity_and_resilient_poll_loop_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            chat_db = tmp_path / "chat.db"
            _init_minimal_chat_db(chat_db)

            cfg = AgentConfig(
                deepseek_api_key="sk-valid-resilient",
                allowed_senders={"+8613800138000"},
                state_dir=state_dir,
                chat_db_path=chat_db,
                poll_interval_sec=0.05,
                cron_interval_sec=5.0,
            )

            poll_calls = 0
            handled_approvals = []

            async def handle(_self, msg):
                handled_approvals.append(msg.text)

            def flaky_poll(_self: MacIMessageBridge) -> list[object]:
                nonlocal poll_calls
                poll_calls += 1
                if poll_calls == 1:
                    raise RuntimeError("simulated transient SQLite / filesystem hiccup")
                os.kill(os.getpid(), signal.SIGTERM)
                return [InboundMessage(1, "approval", "owner@example.test", "test", 0, approval_only=True)]

            prev_hup = signal.getsignal(signal.SIGHUP) if hasattr(signal, "SIGHUP") else None
            try:
                with (
                    mock.patch.object(MacIMessageBridge, "poll_new_messages", flaky_poll),
                    mock.patch.object(HyperTextOrchestrator, "handle_inbound_message", handle),
                ):
                    await asyncio.wait_for(run_imessage_daemon(cfg), timeout=5.0)
                if hasattr(signal, "SIGHUP"):
                    self.assertEqual(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)
            finally:
                if hasattr(signal, "SIGHUP") and prev_hup is not None:
                    signal.signal(signal.SIGHUP, prev_hup)

            self.assertGreaterEqual(poll_calls, 2)
            self.assertEqual(handled_approvals, ["test"])
            self.assertFalse((state_dir / "daemon.pid").exists())

    async def test_unfinished_imessage_is_recovered_from_persisted_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            chat_db = state / "chat.db"
            _init_minimal_chat_db(chat_db)
            cfg = AgentConfig(
                allowed_senders={"owner@example.test"}, state_dir=state,
                chat_db_path=chat_db, poll_interval_sec=0.05, allow_self_chat=True,
            )
            store = StateStore(cfg.sqlite_db_path)
            gate = HumanGate()
            bridge = MacIMessageBridge(store=store, config=cfg, human_gate=gate, durable_inbox=True)
            self.assertEqual(bridge.align_cursor_to_latest(), 0)
            attached_image = state / "sample"
            attached_image.write_bytes(b"synthetic image fixture")
            with sqlite3.connect(chat_db) as conn:
                conn.execute("INSERT INTO handle VALUES (1, 'owner@example.test')")
                conn.execute("CREATE TABLE attachment (filename TEXT, mime_type TEXT)")
                conn.execute("CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER)")
                conn.execute("INSERT INTO attachment VALUES (?, 'image/png')", (str(attached_image),))
                conn.execute("INSERT INTO message_attachment_join VALUES (2, 1)")
                conn.executemany(
                    "INSERT INTO message (guid, text, handle_id) VALUES (?, ?, 1)",
                    [("g1", "/ai finished"), ("g2", "/ai pending"), ("g3", "/ai started")],
                )

            with mock.patch.object(
                store, "record_imessage_batch", side_effect=sqlite3.OperationalError("busy")
            ):
                with self.assertRaises(sqlite3.OperationalError):
                    bridge.poll_new_messages()
            self.assertEqual(store.get_meta(bridge.CURSOR_META_KEY), "0")
            self.assertEqual(store.pending_imessages(), [])
            self.assertEqual(
                [msg.text for msg in bridge.poll_new_messages()], ["finished", "pending", "started"]
            )
            store.complete_imessage(1)
            store.start_imessage(3)
            self.assertNotIn("data:image", store.pending_imessages()[0][0])

            direct_bridge = MacIMessageBridge(
                store=store, config=cfg, human_gate=gate, durable_inbox=True, allow_self_chat=False
            )
            reply = asyncio.create_task(gate.request_input("owner@example.test", "Test approval"))
            try:
                await asyncio.sleep(0)
                with sqlite3.connect(chat_db) as conn:
                    conn.executemany(
                        "INSERT INTO message (guid, text, handle_id) VALUES (?, ?, 1)",
                        [("g4", "123456"), ("g5", "private note")],
                    )
                [approval] = bridge.poll_new_messages()
                self.assertTrue(approval.approval_only)
                with sqlite3.connect(chat_db) as conn:
                    conn.executemany(
                        "INSERT INTO message (guid, text, handle_id) VALUES (?, '123456', 1)",
                        [("g6",), ("g7",)],
                    )
                [approval] = direct_bridge.poll_new_messages()
                self.assertTrue(approval.approval_only)
                self.assertEqual(store.get_meta(bridge.CURSOR_META_KEY), "7")
                self.assertEqual(len(store.pending_imessages()), 2)
                self.assertTrue(all("123456" not in payload for payload, _ in store.pending_imessages()))
            finally:
                reply.cancel()
                await asyncio.gather(reply, return_exceptions=True)
                store.close()

            handled = []
            notices = []

            async def recover(_self, msg):
                handled.append((msg.text, len(msg.image_data_urls)))
                (state / STOP_REQUEST).write_text("test-stop", encoding="ascii")

            async def send_notice(_self, recipient, text):
                notices.append((recipient, text))

            previous_hup = signal.getsignal(signal.SIGHUP)
            try:
                with (
                    mock.patch.object(HyperTextOrchestrator, "handle_inbound_message", recover),
                    mock.patch.object(MacIMessageBridge, "send_text", send_notice),
                ):
                    await asyncio.wait_for(run_imessage_daemon(cfg), timeout=5)
            finally:
                signal.signal(signal.SIGHUP, previous_hup)
            self.assertEqual(handled, [("pending", 1)])
            self.assertEqual(len(notices), 1)
            self.assertIn("核对结果", notices[0][1])
            store = StateStore(cfg.sqlite_db_path)
            try:
                self.assertEqual(store.pending_imessages(), [])
            finally:
                store.close()

    async def test_pending_gate_keeps_self_chat_controls_and_new_requests_durable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            db = state / "chat.db"
            _init_minimal_chat_db(db)
            sender = "owner@example.test"
            cfg = AgentConfig(
                allowed_senders={sender}, state_dir=state, chat_db_path=db, allow_self_chat=True,
            )
            gate = HumanGate()
            with contextlib.closing(StateStore(cfg.sqlite_db_path)) as store:
                bridge = MacIMessageBridge(config=cfg, store=store, human_gate=gate, durable_inbox=True)
                orch = HyperTextOrchestrator(config=cfg, store=store, bridge=bridge, human_gate=gate)
                bridge.align_cursor_to_latest()

                async def start_gate():
                    task = asyncio.create_task(gate.request_input(sender, "Test approval"))
                    await asyncio.sleep(0)
                    return task

                original = await start_gate()
                try:
                    with contextlib.closing(sqlite3.connect(db)) as conn, conn:
                        conn.execute("INSERT INTO handle VALUES (1, ?)", (sender,))
                        conn.executemany(
                            "INSERT INTO message (text, handle_id) VALUES (?, 1)",
                            [("/ai 新任务",), ("私人备忘",), ("/status",), ("停",), ("123456",), ("再发一份备忘",)],
                        )
                    polled = bridge.poll_new_messages()
                    self.assertEqual([msg.text for msg in polled], ["新任务", "/status", "停", "123456"])
                    self.assertEqual([msg.approval_only for msg in polled], [False, False, False, True])
                    self.assertEqual(
                        [json.loads(payload)["text"] for payload, _ in store.pending_imessages()],
                        ["新任务", "/status", "停"],
                    )
                finally:
                    original.cancel()
                    await asyncio.gather(original, return_exceptions=True)

                next_gate = await start_gate()
                try:
                    await orch.handle_inbound_message(polled[-1])
                    self.assertFalse(next_gate.done())
                    with mock.patch.object(bridge, "send_text", new_callable=mock.AsyncMock), mock.patch.object(
                        orch, "_run_agent_turn", new_callable=mock.AsyncMock, return_value="完成"
                    ) as run_turn:
                        self.assertEqual(await orch.handle_inbound_message(polled[0]), "完成")
                        run_turn.assert_awaited_once()
                    self.assertTrue(gate.has_pending(sender))
                finally:
                    gate.close()
                    await asyncio.gather(next_gate, return_exceptions=True)

    def test_self_chat_mirror_after_restart_does_not_enqueue_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            db = state / "chat.db"
            _init_minimal_chat_db(db)
            sender = "owner@example.test"
            cfg = AgentConfig(
                allowed_senders={sender}, state_dir=state, chat_db_path=db, allow_self_chat=True,
            )
            source_stamp = int(time.time() - APPLE_EPOCH_OFFSET)
            with contextlib.closing(StateStore(cfg.sqlite_db_path)) as store:
                first = MacIMessageBridge(config=cfg, store=store, durable_inbox=True)
                first.align_cursor_to_latest()
                with contextlib.closing(sqlite3.connect(db)) as conn, conn:
                    conn.execute("INSERT INTO handle VALUES (1, ?)", (sender,))
                    conn.execute(
                        "INSERT INTO message (guid,text,handle_id,is_from_me,date) VALUES "
                        "('send', '/ai 测试镜像', 1, 1, ?)", (source_stamp,),
                    )
                [msg] = first.poll_new_messages()
                store.start_imessage(msg.rowid)

            with contextlib.closing(sqlite3.connect(db)) as conn, conn:
                conn.execute(
                    "INSERT INTO message (guid,text,handle_id,is_from_me,date) VALUES "
                    "('mirror', '/ai 测试镜像', 1, 0, ?)", (source_stamp + 1,),
                )
            with contextlib.closing(StateStore(cfg.sqlite_db_path)) as store:
                bridge = MacIMessageBridge(config=cfg, store=store, durable_inbox=True)
                self.assertEqual(bridge.poll_new_messages(), [])
                self.assertEqual(store.get_meta(bridge.CURSOR_META_KEY), "2")
                self.assertEqual(len(store.pending_imessages()), 1)

                # A fresh, identical instruction and its own mirror form a new turn.
                with contextlib.closing(sqlite3.connect(db)) as conn, conn:
                    conn.executemany(
                        "INSERT INTO message (guid,text,handle_id,is_from_me,date) VALUES "
                        "(?, '/ai 测试镜像', 1, ?, ?)",
                        [("again", 1, source_stamp + 2), ("again-mirror", 0, source_stamp + 3)],
                    )
                [new_request] = bridge.poll_new_messages()
                self.assertEqual(new_request.guid, "again")

    async def test_delivery_failure_retires_row_and_interrupted_notice_retries_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            db = state / "chat.db"
            _init_minimal_chat_db(db)
            cfg = AgentConfig(
                allowed_senders={"owner@example.test"},
                state_dir=state,
                chat_db_path=db,
                poll_interval_sec=0.05,
            )
            with contextlib.closing(StateStore(cfg.sqlite_db_path)) as store:
                rows = [
                    (
                        i,
                        json.dumps(
                            asdict(
                                InboundMessage(
                                    i,
                                    str(i),
                                    "owner@example.test",
                                    "test",
                                    0,
                                    service="iMessage",
                                )
                            )
                        ),
                    )
                    for i in (1, 2)
                ]
                store.record_imessage_batch(MacIMessageBridge.CURSOR_META_KEY, 2, rows)
                store.start_imessage(2)

            notices = []

            async def unreachable(_self, msg):
                notices.append(msg.rowid)
                if len(notices) == 3:
                    (state / STOP_REQUEST).write_text("test-stop", encoding="ascii")
                raise MessageDeliveryError("unreachable")

            previous_hup = signal.getsignal(signal.SIGHUP)
            try:
                with (
                    mock.patch.object(HyperTextOrchestrator, "notify_interrupted_imessage", unreachable),
                    mock.patch.object(
                        HyperTextOrchestrator, "handle_inbound_message",
                        side_effect=MessageDeliveryError("delivery failed"),
                    ) as handle,
                    mock.patch("hypertext.cli.INTERRUPTION_NOTICE_RETRY_SEC", 0.01),
                ):
                    await asyncio.wait_for(run_imessage_daemon(cfg), timeout=4)
                self.assertEqual([call.args[0].rowid for call in handle.await_args_list], [1])
            finally:
                signal.signal(signal.SIGHUP, previous_hup)
            self.assertEqual(notices, [2, 2, 2])
            with contextlib.closing(StateStore(cfg.sqlite_db_path)) as store:
                self.assertEqual(store.pending_imessages(), [])

    async def test_shutdown_closes_live_approval_without_waiting_for_gate_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            db = state / "chat.db"
            _init_minimal_chat_db(db)
            cfg = AgentConfig(
                allowed_senders={"owner@example.test"},
                state_dir=state,
                chat_db_path=db,
                human_gate_timeout_sec=300,
            )
            with contextlib.closing(StateStore(cfg.sqlite_db_path)) as store:
                msg = InboundMessage(
                    1,
                    "approval-test",
                    "owner@example.test",
                    "test",
                    0,
                    service="iMessage",
                )
                store.record_imessage_batch(
                    MacIMessageBridge.CURSOR_META_KEY, 1, [(1, json.dumps(asdict(msg)))]
                )

            entered = asyncio.Event()
            cancelled = asyncio.Event()

            async def wait_for_approval(orch, msg):
                entered.set()
                try:
                    await orch.human_gate.request_input(msg.sender, "test approval")
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

            async def stop_when_waiting():
                await entered.wait()
                (state / STOP_REQUEST).write_text("test-stop", encoding="ascii")

            previous_hup = signal.getsignal(signal.SIGHUP)
            try:
                with mock.patch.object(
                    HyperTextOrchestrator, "handle_inbound_message", wait_for_approval
                ):
                    await asyncio.wait_for(asyncio.gather(
                        run_imessage_daemon(cfg), stop_when_waiting()
                    ), timeout=3)
            finally:
                signal.signal(signal.SIGHUP, previous_hup)
            self.assertTrue(cancelled.is_set())
            self.assertFalse((state / "daemon.pid").exists())
            with contextlib.closing(StateStore(cfg.sqlite_db_path)) as store:
                self.assertTrue(store.pending_imessages()[0][1])

    def test_legacy_database_and_logs_are_private_and_new_log_records_are_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "state" / "test.db"
            with contextlib.closing(StateStore(db)):
                self.assertEqual(db.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(db.stat().st_mode & 0o777, 0o600)
            db.chmod(0o644)
            with contextlib.closing(StateStore(db)):
                self.assertEqual(db.stat().st_mode & 0o777, 0o600)

            log_path = root / "daemon.log"
            archived = root / "daemon.log.1"
            log_path.write_text("", encoding="utf-8")
            archived.write_text("", encoding="utf-8")
            for path in (log_path, archived):
                path.chmod(0o644)
            log = DaemonLog(log_path)
            log.protect_existing()
            log.write(
                b"Bearer fakeBearer10 client_secret=fakeClient10 HT-FAKECODE "
                b"socks5://proxyuser:fakeProxyPassword@proxy.example.test "
                b"https://example.test/?token=fakeQueryToken&x=1 2125550123\n"
            )
            for str_value in (
                "fakeBearer10", "fakeClient10", "HT-FAKECODE", "fakeProxyPassword",
                "fakeQueryToken", "2125550123",
            ):
                self.assertNotIn(str_value, log_path.read_text(encoding="utf-8"))
            for path in (log_path, archived):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_start_daemon_sh_lifecycle_auto_respawn_and_redacted_status(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        script = repo_root / "scripts" / "start_daemon.sh"
        self.assertTrue(script.is_file())
        self.assertTrue(os.access(script, os.X_OK), "scripts/start_daemon.sh must be executable")

        syntax = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True, check=False
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "isolated_state"
            dummy_worker = tmp_path / "dummy_hypertext_worker.py"
            dummy_worker.write_text(
                "import os, pathlib, signal, sys, time\n"
                f"sys.path.insert(0, {str(repo_root)!r})\n"
                "from hypertext.cli import acquire_daemon_lock\n"
                "sd = pathlib.Path(os.environ['HYPERTEXT_STATE_DIR'])\n"
                "stop = False\n"
                "def _stop(*_a):\n"
                "    global stop\n"
                "    stop = True\n"
                "    if (sd / 'slow-stop').exists():\n"
                "        time.sleep(0.8)\n"
                "        (sd / 'drained').touch()\n"
                "signal.signal(signal.SIGTERM, _stop)\n"
                "signal.signal(signal.SIGINT, _stop)\n"
                "if hasattr(signal, 'SIGHUP'):\n"
                "    signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
                "with acquire_daemon_lock(sd) as fd:\n"
                "    if fd is None:\n"
                "        raise SystemExit(0)\n"
                "    print('Booted worker key=sk-fakesecret123456 relay=xn_fakerelay987654 user=test_user@example.com phone=+8613800138000', flush=True)\n"
                "    while not stop:\n"
                "        if (sd / 'large-log').exists():\n"
                "            (sd / 'large-log').unlink()\n"
                "            for _ in range(704):\n"
                "                os.write(1, b'x' * 32768 + b'\\n')\n"
                "            print('end of large log', flush=True)\n"
                "        time.sleep(0.05)\n",
                encoding="utf-8",
            )

            env = dict(os.environ)
            env["HYPERTEXT_STATE_DIR"] = str(state_dir)
            env["HYPERTEXT_AGENT_SCRIPT"] = str(dummy_worker)
            env["HYPERTEXT_PYTHON_BIN"] = sys.executable
            env["HYPERTEXT_RESPAWN_DELAY_SEC"] = "0.2"
            env["HYPERTEXT_DISABLE_CAFFEINATE"] = "1"

            try:
                def start(_index):
                    return subprocess.run(
                        [str(script), "start"], env=env, capture_output=True,
                        text=True, timeout=10, check=False,
                    )

                with ThreadPoolExecutor(max_workers=8) as pool:
                    starts = list(pool.map(start, range(8)))
                for result in starts:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(sum("supervisor started" in r.stdout for r in starts), 1)

                pid_file = state_dir / "daemon.pid"
                sup_file = state_dir / "supervisor.pid"
                for _ in range(30):
                    if pid_file.is_file() and sup_file.is_file():
                        break
                    time.sleep(0.1)
                self.assertTrue(pid_file.is_file())
                self.assertTrue(sup_file.is_file())
                first_worker_pid = int(pid_file.read_text(encoding="utf-8").strip())
                sup_pid = int(sup_file.read_text(encoding="utf-8").strip())

                # Second `start` is idempotent
                r_dup = subprocess.run(
                    [str(script), "start"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(r_dup.returncode, 0)
                self.assertIn("already running", r_dup.stdout)
                self.assertEqual(int(pid_file.read_text(encoding="utf-8").strip()), first_worker_pid)

                # `status` reports running & redacts secrets/emails/phones
                r_status = subprocess.run(
                    [str(script), "status"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(r_status.returncode, 0, r_status.stdout + r_status.stderr)
                self.assertIn(f"Supervisor : running (PID={sup_pid})", r_status.stdout)
                self.assertIn("Worker     : running", r_status.stdout)
                self.assertIn("sk-***", r_status.stdout)
                self.assertIn("xn_***", r_status.stdout)
                self.assertIn("[redacted-email]", r_status.stdout)
                self.assertIn("[redacted-phone]", r_status.stdout)
                for secret_tok in (
                    "sk-fakesecret123456",
                    "xn_fakerelay987654",
                    "test_user@example.com",
                    "+8613800138000",
                ):
                    self.assertNotIn(secret_tok, r_status.stdout)

                # Kill worker abruptly -> supervisor MUST self-heal and respawn a new worker PID!
                os.kill(first_worker_pid, signal.SIGKILL)
                new_worker_pid = first_worker_pid
                for _ in range(30):
                    time.sleep(0.1)
                    if pid_file.is_file():
                        try:
                            cand_pid = int(pid_file.read_text(encoding="utf-8").strip())
                            if cand_pid != first_worker_pid:
                                os.kill(cand_pid, 0)
                                new_worker_pid = cand_pid
                                break
                        except (ValueError, OSError):
                            continue
                self.assertNotEqual(new_worker_pid, first_worker_pid)
                log_txt = (state_dir / "daemon.log").read_text(encoding="utf-8")
                self.assertIn("[Supervisor] Worker PID", log_txt)
                self.assertIn("respawning", log_txt)

                (state_dir / "large-log").touch()
                for _ in range(60):
                    with (state_dir / "daemon.log").open("rb") as log:
                        log.seek(max(0, (state_dir / "daemon.log").stat().st_size - 2048))
                        if b"end of large log" in log.read(2048):
                            break
                    time.sleep(0.05)
                else:
                    self.fail("Worker did not finish writing the synthetic log")
                self.assertTrue((state_dir / "daemon.log.1").is_file())
                self.assertLessEqual((state_dir / "daemon.log").stat().st_size, MAX_LOG_BYTES)
                self.assertEqual(int(pid_file.read_text(encoding="utf-8").strip()), new_worker_pid)

                (state_dir / "slow-stop").touch()
                early_restart = subprocess.run(
                    [str(script), "restart"],
                    env=dict(env, HYPERTEXT_SHUTDOWN_TIMEOUT_SEC="0.1"),
                    capture_output=True, text=True, timeout=5, check=False,
                )
                self.assertNotEqual(early_restart.returncode, 0)
                self.assertIn("still stopping", early_restart.stderr)
                self.assertEqual(int(pid_file.read_text(encoding="utf-8").strip()), new_worker_pid)
            finally:
                r_stop = subprocess.run(
                    [str(script), "stop"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(r_stop.returncode, 0)

            self.assertTrue((state_dir / "drained").is_file())

            r_status_after = subprocess.run(
                [str(script), "status"],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(r_status_after.returncode, 1)
            self.assertIn("Worker     : stopped", r_status_after.stdout)

    def test_launcher_uses_selected_config_for_every_command(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        script = repo_root / "scripts" / "start_daemon.sh"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = dict(
                os.environ,
                HYPERTEXT_STATE_DIR=str(root / "ambient"),
                HYPERTEXT_PYTHON_BIN=sys.executable,
                HYPERTEXT_AGENT_SCRIPT=str(repo_root / "hypertext_agent.py"),
                HYPERTEXT_DISABLE_CAFFEINATE="1",
            )
            chat_db = root / "chat.db"
            _init_minimal_chat_db(chat_db)

            def command(action, instance):
                return subprocess.run(
                    [str(script), action, "--env-file", str(root / f"{instance}.env")],
                    env=env, capture_output=True, text=True, timeout=10, check=False,
                )

            for instance in ("first", "second"):
                (root / f"{instance}.env").write_text(
                    f"HYPERTEXT_STATE_DIR={root / instance}\nCHAT_DB_PATH={chat_db}\n",
                    encoding="utf-8",
                )
            doctor = command("--doctor", "first")
            self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
            self.assertIn("诊断完成", doctor.stdout)
            self.assertFalse((root / "first" / "supervisor.pid").exists())
            (root / "first").chmod(0o755)

            with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]) as unrelated:
                try:
                    (root / "first" / "daemon.pid").write_text(str(unrelated.pid), encoding="ascii")
                    (root / "first" / "supervisor.pid").write_text(str(unrelated.pid), encoding="ascii")
                    stale_stop = command("stop", "first")
                    self.assertEqual(stale_stop.returncode, 0, stale_stop.stdout + stale_stop.stderr)
                    self.assertIsNone(unrelated.poll())

                    for instance in ("first", "second"):
                        started = command("start", instance)
                        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
                        self.assertTrue((root / instance / "daemon.pid").is_file())
                        self.assertEqual((root / instance).stat().st_mode & 0o777, 0o700)
                    stopped = command("stop", "first")
                    self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
                    self.assertEqual(command("status", "first").returncode, 1)
                    self.assertEqual(command("status", "second").returncode, 0)
                    self.assertFalse((root / "ambient").exists())

                    supervisor_pid = int((root / "second" / "supervisor.pid").read_text())
                    os.kill(supervisor_pid, signal.SIGKILL)
                    deadline = time.monotonic() + 3
                    while (root / "second" / "daemon.pid").exists() and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertFalse((root / "second" / "daemon.pid").exists())
                    self.assertEqual(command("status", "second").returncode, 1)
                finally:
                    command("stop", "first")
                    command("stop", "second")
                    unrelated.terminate()

    def test_codex_review_sh_and_agents_md_contract(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        codex_script = repo_root / "scripts" / "codex_review.sh"
        agents_md = repo_root / "AGENTS.md"

        self.assertTrue(codex_script.is_file())
        self.assertTrue(os.access(codex_script, os.X_OK), "scripts/codex_review.sh must be executable")
        syntax = subprocess.run(
            ["bash", "-n", str(codex_script)], capture_output=True, text=True, check=False
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

        with tempfile.TemporaryDirectory() as tmp:
            fake_codex = Path(tmp) / "codex"
            fake_codex.write_text("#!/bin/sh\necho fake-codex \"$@\"\n", encoding="utf-8")
            fake_codex.chmod(0o755)

            env = dict(os.environ)
            env.pop("HTTPS_PROXY", None)
            env.pop("https_proxy", None)
            env["CODEX_CLI_PATH"] = str(fake_codex)
            env["HYPERTEXT_LOCAL_PROXY"] = "http://review-user:fake-password@127.0.0.1:7897"
            env["HYPERTEXT_CODEX_REVIEW_DRY_RUN"] = "1"

            res = subprocess.run(
                [str(codex_script), "--commit", "HEAD"],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn(f"CODEX_BIN={fake_codex}", res.stdout)
            self.assertIn("127.0.0.1,localhost,::1", res.stdout)
            self.assertIn("HTTPS_PROXY=configured", res.stdout)
            self.assertNotIn("fake-password", res.stdout)
            self.assertIn("ARGS=--commit HEAD", res.stdout)

        self.assertTrue(agents_md.is_file())
        agents_txt = agents_md.read_text(encoding="utf-8")
        for required_phrase in (
            "Single-Exit",
            "Zero Plumbing on Screen",
            "HYPERTEXT_MAX_HEARTBEAT_PER_TURN=1",
            "_is_admin_sender",
            "scripts/start_daemon.sh",
            "scripts/codex_review.sh",
            "daemon.lock",
        ):
            self.assertIn(required_phrase, agents_txt)
        home_str = os.path.expanduser("~").replace("\\", "/")
        self.assertNotIn(home_str, agents_txt)


if __name__ == "__main__":
    unittest.main()
