"""Tests for HyperText v0.6.3 Self-Healing Daemon Supervisor, `flock` Single-Instance Lock, `AGENTS.md`, and `codex_review.sh`."""

from __future__ import annotations

import asyncio
import contextlib
import io
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
    MacIMessageBridge,
    acquire_daemon_lock,
    main as cli_main,
)
from hypertext.cli import run_imessage_daemon


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

            def flaky_poll(_self: MacIMessageBridge) -> list[object]:
                nonlocal poll_calls
                poll_calls += 1
                if poll_calls == 1:
                    raise RuntimeError("simulated transient SQLite / filesystem hiccup")
                os.kill(os.getpid(), signal.SIGTERM)
                return []

            prev_hup = signal.getsignal(signal.SIGHUP) if hasattr(signal, "SIGHUP") else None
            try:
                with mock.patch.object(MacIMessageBridge, "poll_new_messages", flaky_poll):
                    await asyncio.wait_for(run_imessage_daemon(cfg), timeout=5.0)
                if hasattr(signal, "SIGHUP"):
                    self.assertEqual(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)
            finally:
                if hasattr(signal, "SIGHUP") and prev_hup is not None:
                    signal.signal(signal.SIGHUP, prev_hup)

            self.assertGreaterEqual(poll_calls, 2)
            self.assertFalse((state_dir / "daemon.pid").exists())

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
                "signal.signal(signal.SIGTERM, _stop)\n"
                "signal.signal(signal.SIGINT, _stop)\n"
                "if hasattr(signal, 'SIGHUP'):\n"
                "    signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
                "with acquire_daemon_lock(sd) as fd:\n"
                "    if fd is None:\n"
                "        raise SystemExit(0)\n"
                "    print('Booted worker key=sk-fakesecret123456 relay=xn_fakerelay987654 user=test_user@example.com phone=+8613800138000', flush=True)\n"
                "    while not stop:\n"
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
                r_start = subprocess.run(
                    [str(script), "start"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(r_start.returncode, 0, r_start.stdout + r_start.stderr)

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
                self.assertEqual(r_status.returncode, 0, r_status.stdout)
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
            env["HYPERTEXT_LOCAL_PROXY"] = "http://127.0.0.1:7897"
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
            self.assertIn("HTTPS_PROXY=http://127.0.0.1:7897", res.stdout)
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
