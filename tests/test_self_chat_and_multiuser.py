from __future__ import annotations

import asyncio
import contextlib
import io
import os
import pathlib
import re
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock

from hypertext import (
    BOT_ECHO_MARKER,
    HyperTextOrchestrator,
    InstinctOrchestrator,
    __version__,
)
from hypertext.bridge_imessage import InboundMessage, MacIMessageBridge, MessageBridge
from hypertext.cli import main as cli_main
from hypertext.config import AgentConfig, normalize_sender
from hypertext.human_gate import HumanGate
from hypertext.sandbox_browser import BrowserSandbox
from hypertext.store_sqlite import StateStore


def _init_realistic_chat_db(db_path: pathlib.Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE handle (
            ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL
        );
        CREATE TABLE chat (
            ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_identifier TEXT NOT NULL
        );
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
            handle_id INTEGER DEFAULT 0,
            text TEXT,
            attributedBody BLOB,
            is_from_me INTEGER DEFAULT 0,
            associated_message_type INTEGER DEFAULT 0,
            cache_has_attachments INTEGER DEFAULT 0,
            date INTEGER DEFAULT 0
        );
        CREATE TABLE chat_message_join (
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL
        );
        CREATE TABLE attachment (
            ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            mime_type TEXT,
            transfer_name TEXT,
            total_bytes INTEGER DEFAULT 0
        );
        CREATE TABLE message_attachment_join (
            message_id INTEGER NOT NULL,
            attachment_id INTEGER NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


def _insert_chat_msg(
    db_path: pathlib.Path,
    *,
    sender_handle: str,
    text: str,
    is_from_me: int,
    attachment_path: pathlib.Path | None = None,
) -> int:
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("SELECT ROWID FROM handle WHERE id = ?", (sender_handle,))
    row = cur.fetchone()
    if row:
        handle_id = int(row[0])
    else:
        cur.execute("INSERT INTO handle (id) VALUES (?)", (sender_handle,))
        handle_id = int(cur.lastrowid)

    cur.execute("SELECT ROWID FROM chat WHERE chat_identifier = ?", (sender_handle,))
    crow = cur.fetchone()
    if crow:
        chat_id = int(crow[0])
    else:
        cur.execute("INSERT INTO chat (chat_identifier) VALUES (?)", (sender_handle,))
        chat_id = int(cur.lastrowid)

    has_att = 1 if attachment_path is not None else 0
    cur.execute(
        """
        INSERT INTO message (handle_id, text, is_from_me, associated_message_type, cache_has_attachments)
        VALUES (?, ?, ?, 0, ?)
        """,
        (handle_id, text, is_from_me, has_att),
    )
    msg_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)",
        (chat_id, msg_id),
    )
    if attachment_path is not None:
        cur.execute(
            """
            INSERT INTO attachment (filename, mime_type, transfer_name, total_bytes)
            VALUES (?, 'image/png', ?, ?)
            """,
            (
                str(attachment_path),
                attachment_path.name,
                attachment_path.stat().st_size,
            ),
        )
        att_id = int(cur.lastrowid)
        cur.execute(
            "INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (?, ?)",
            (msg_id, att_id),
        )
    conn.commit()
    conn.close()
    return msg_id


class _InMemoryBridge(MessageBridge):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def poll_new_messages(self) -> list[InboundMessage]:
        return []

    async def send_text(self, recipient: str, text: str) -> None:
        self.sent.append((normalize_sender(recipient), text))


def _mk_inbound(rowid: int, sender: str, text: str, is_from_me: int = 0) -> InboundMessage:
    return InboundMessage(
        rowid=rowid,
        guid=f"guid-{rowid}",
        sender=sender,
        text=text,
        timestamp_unix=1700000000.0 + rowid,
        is_from_me=is_from_me,
    )


def _make_fake_llm_response(content: str = "收到，已处理！") -> types.SimpleNamespace:
    msg = types.SimpleNamespace(
        content=content,
        reasoning_content="简要思考过程",
        tool_calls=None,
        server_tool_calls=None,
    )
    choice = types.SimpleNamespace(message=msg)
    return types.SimpleNamespace(choices=[choice])


class TestSelfChatAndMultiUser(unittest.IsolatedAsyncioTestCase):
    async def test_self_chat_prefix_word_boundary_plain_notes_and_other_contacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            chat_db = tmp_path / "chat.db"
            _init_realistic_chat_db(chat_db)

            img_file = tmp_path / "receipt.png"
            img_file.write_bytes(b"\x89PNG\r\n\x1a\nfakepng")

            admin = "+8613800138000"
            friend = "+8613999999999"

            cfg = AgentConfig(
                state_dir=tmp_path / "state",
                chat_db_path=chat_db,
                allowed_senders={normalize_sender(admin)},
                allow_self_chat=True,
                self_chat_prefix="/ai",
                deepseek_api_key="sk-testvalidkey123456",
            )
            store = StateStore(cfg.state_dir / "state.db")
            gate = HumanGate()
            bridge = MacIMessageBridge(config=cfg, store=store, human_gate=gate)

            # Initialize cursor at 0
            self.assertEqual(bridge.poll_new_messages(), [])

            # 1. Plain self-note (both is_from_me=1 and mirror is_from_me=0) -> ignored
            _insert_chat_msg(chat_db, sender_handle=admin, text="买牛奶和鸡蛋", is_from_me=1)
            _insert_chat_msg(chat_db, sender_handle=admin, text="买牛奶和鸡蛋", is_from_me=0)

            # 2. Prefix word boundary (/airdrop should NOT match /ai)
            _insert_chat_msg(chat_db, sender_handle=admin, text="/airdrop 传照片", is_from_me=1)

            # 3. Outbound is_from_me=1 sent to a DIFFERENT contact -> NEVER trigger!
            _insert_chat_msg(chat_db, sender_handle=friend, text="/ai 帮我查天气", is_from_me=1)

            self.assertEqual(bridge.poll_new_messages(), [])

            # 4. Valid case-insensitive /AI command -> stripped cleanly
            _insert_chat_msg(chat_db, sender_handle=admin, text="/AI   查一下明天上海天气", is_from_me=1)
            msgs = bridge.poll_new_messages()
            self.assertEqual(len(msgs), 1)
            self.assertEqual(msgs[0].text, "查一下明天上海天气")
            self.assertEqual(normalize_sender(msgs[0].sender), normalize_sender(admin))

            # 5. Bare '/ai' WITH an image attachment -> accepted!
            _insert_chat_msg(
                chat_db,
                sender_handle=admin,
                text="/ai",
                is_from_me=1,
                attachment_path=img_file,
            )
            img_msgs = bridge.poll_new_messages()
            self.assertEqual(len(img_msgs), 1)
            self.assertEqual(len(img_msgs[0].image_data_urls), 1)
            store.close()

    async def test_self_chat_twin_row_dedup_and_4layer_echo_suppression(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            chat_db = tmp_path / "chat.db"
            _init_realistic_chat_db(chat_db)

            admin = "+8613800138000"
            cfg = AgentConfig(
                state_dir=tmp_path / "state",
                chat_db_path=chat_db,
                allowed_senders={normalize_sender(admin)},
                allow_self_chat=True,
                self_chat_prefix="/ai",
                deepseek_api_key="sk-testvalidkey123456",
            )
            store = StateStore(cfg.state_dir / "state.db")
            bridge = MacIMessageBridge(config=cfg, store=store)
            bridge.poll_new_messages()

            # Twin rows inserted by macOS for a single self-sent message (is_from_me=1 + 0)
            _insert_chat_msg(chat_db, sender_handle=admin, text="/ai 整理本周待办", is_from_me=1)
            _insert_chat_msg(chat_db, sender_handle=admin, text="/ai 整理本周待办", is_from_me=0)
            deduped = bridge.poll_new_messages()
            self.assertEqual(len(deduped), 1)
            self.assertEqual(deduped[0].text, "整理本周待办")

            # Verify send_text prepends BOT_ECHO_MARKER (\u2063) and records SHA-256 outbound echo
            proc_mock = mock.AsyncMock()
            proc_mock.communicate = mock.AsyncMock(return_value=(b"", b""))
            proc_mock.returncode = 0
            with mock.patch("asyncio.create_subprocess_exec", return_value=proc_mock) as exec_mock:
                await bridge.send_text(admin, "/ai 假装包含前缀的AI回复正文")
                self.assertTrue(exec_mock.called)
                wire_arg = exec_mock.call_args[0][4]
                self.assertTrue(wire_arg.startswith(BOT_ECHO_MARKER))
                self.assertLessEqual(len(wire_arg), 3500)

            # Echoes arriving back in chat.db (with U+2063, stripped U+2063 matching hash, and bot prefixes)
            _insert_chat_msg(
                chat_db,
                sender_handle=admin,
                text=f"{BOT_ECHO_MARKER}/ai 假装包含前缀的AI回复正文",
                is_from_me=1,
            )
            _insert_chat_msg(
                chat_db,
                sender_handle=admin,
                text="/ai 假装包含前缀的AI回复正文",
                is_from_me=0,
            )
            for prefix_msg in (
                "🔐 [需要验证码] 请输入短信码",
                "🌐 [浏览器进度] 正在打开页面",
                "⚠️ 今日额度已满",
                "🎉 邀请码激活成功！",
                "👋 你好，当前 AI 助理处于邀请内测阶段",
            ):
                _insert_chat_msg(chat_db, sender_handle=admin, text=prefix_msg, is_from_me=0)
                _insert_chat_msg(chat_db, sender_handle=admin, text=prefix_msg, is_from_me=1)

            self.assertEqual(bridge.poll_new_messages(), [])
            store.close()

    async def test_self_chat_bare_2fa_code_unlocks_humangate_without_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            chat_db = tmp_path / "chat.db"
            _init_realistic_chat_db(chat_db)

            admin = "+8613800138000"
            cfg = AgentConfig(
                state_dir=tmp_path / "state",
                chat_db_path=chat_db,
                allowed_senders={normalize_sender(admin)},
                allow_self_chat=True,
                self_chat_prefix="/ai",
                deepseek_api_key="sk-testvalidkey123456",
            )
            store = StateStore(cfg.state_dir / "state.db")
            gate = HumanGate()
            bridge = MacIMessageBridge(config=cfg, store=store)

            llm_mock = types.SimpleNamespace(
                chat=types.SimpleNamespace(
                    completions=types.SimpleNamespace(
                        create=mock.AsyncMock(return_value=_make_fake_llm_response("OK"))
                    )
                )
            )
            orch = HyperTextOrchestrator(
                config=cfg,
                store=store,
                bridge=bridge,
                human_gate=gate,
                llm_client=llm_mock,
            )
            # Orchestrator automatically attaches human_gate to bridge
            self.assertIs(bridge.human_gate, gate)
            bridge.poll_new_messages()

            wait_task = asyncio.create_task(
                gate.request_input(admin, "请输入6位验证码", timeout=5.0)
            )
            await asyncio.sleep(0.02)
            self.assertTrue(gate.has_pending(admin))

            # User sends bare 2FA code WITHOUT '/ai' (both is_from_me=1 and mirror 0)
            _insert_chat_msg(chat_db, sender_handle=admin, text="849201", is_from_me=1)
            _insert_chat_msg(chat_db, sender_handle=admin, text="849201", is_from_me=0)

            inbound = bridge.poll_new_messages()
            self.assertEqual(len(inbound), 1)
            self.assertEqual(inbound[0].text, "849201")

            await orch.handle_inbound_message(inbound[0])
            code_result = await wait_task
            self.assertEqual(code_result, "849201")
            # Ensure normal LLM chat was NOT invoked for the 2FA code
            llm_mock.chat.completions.create.assert_not_called()
            store.close()

    async def test_per_user_browser_profile_isolation_and_concurrency_pool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            cfg = AgentConfig(
                state_dir=tmp_path / "state",
                browser_max_concurrency=2,
                deepseek_api_key="sk-testvalidkey123456",
            )
            gate = HumanGate()
            mem_bridge = _InMemoryBridge()
            sandbox = BrowserSandbox(config=cfg, human_gate=gate, bridge=mem_bridge)

            # Backward-compatible property
            self.assertEqual(sandbox.profile_dir, cfg.state_dir / "browser_profile")

            # Per-sender hashed isolation with zero PII
            p_a1 = sandbox.profile_dir_for("+86 138-0013-8000")
            p_a2 = sandbox.profile_dir_for("+8613800138000")
            p_b = sandbox.profile_dir_for("friend@icloud.com")
            self.assertEqual(p_a1, p_a2)
            self.assertNotEqual(p_a1, p_b)
            self.assertRegex(p_a1.name, r"^user_[0-9a-f]{16}$")
            self.assertNotIn("13800138000", str(p_a1))
            self.assertNotIn("friend", str(p_b))

            # Concurrency test: userA & userB overlap concurrently; two userA tasks serialize
            seen_dirs: list[str] = []
            active_total = 0
            max_active_total = 0
            active_per_dir: dict[str, int] = {}
            max_per_dir: dict[str, int] = {}
            both_users_entered = asyncio.Event()

            class FakeBrowser:
                def __init__(self, *args, **kwargs):
                    self.args = args
                    self.user_data_dir = kwargs.get("user_data_dir", "")
                    seen_dirs.append(self.user_data_dir)

                async def stop(self) -> None:
                    return None

            class FakeChatOpenAI:
                def __init__(self, *args, **kwargs):
                    pass

            class FakeTools:
                def action(self, _desc: str):
                    def _decorator(fn):
                        return fn
                    return _decorator

            class FakeBrowserAgent:
                def __init__(self, *, task, llm, browser, tools, **kwargs):
                    self.browser = browser
                    self.task = task

                async def run(self, max_steps: int = 25):
                    nonlocal active_total, max_active_total
                    d = self.browser.user_data_dir
                    active_total += 1
                    max_active_total = max(max_active_total, active_total)
                    active_per_dir[d] = active_per_dir.get(d, 0) + 1
                    max_per_dir[d] = max(max_per_dir.get(d, 0), active_per_dir[d])
                    if active_total >= 2:
                        both_users_entered.set()
                    await asyncio.sleep(0.06)
                    active_per_dir[d] -= 1
                    active_total -= 1
                    return types.SimpleNamespace(final_result=lambda: f"done:{self.task}")

            fake_bu = types.ModuleType("browser_use")
            fake_bu.Agent = FakeBrowserAgent
            fake_bu.Browser = FakeBrowser
            fake_bu.ChatOpenAI = FakeChatOpenAI
            fake_bu.Tools = FakeTools

            with mock.patch.dict(sys.modules, {"browser_use": fake_bu}):
                res = await asyncio.gather(
                    sandbox.run_task("+8613800138000", "uA-1"),
                    sandbox.run_task("friend@icloud.com", "uB-1"),
                    sandbox.run_task("+8613800138000", "uA-2"),
                )

            self.assertEqual(len(res), 3)
            self.assertTrue(both_users_entered.is_set(), "Different users should run concurrently")
            self.assertEqual(max_active_total, 2)
            for d, peak in max_per_dir.items():
                self.assertEqual(peak, 1, f"Same user profile {d} must never exceed concurrency 1")

    async def test_invite_code_lifecycle_24h_antispam_and_daily_quotas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            admin = "+8613800138000"
            member1 = "+8613700001111"
            member2 = "+8613700002222"

            cfg = AgentConfig(
                state_dir=tmp_path / "state",
                allowed_senders={normalize_sender(admin)},
                allow_self_chat=True,
                self_chat_prefix="/ai",
                enable_invite_codes=True,
                invite_codes=["HT-TEAM99", "INST-TEAM99"],
                default_member_daily_msg_limit=2,
                default_member_daily_browser_limit=1,
                deepseek_api_key="sk-testvalidkey123456",
            )
            store = StateStore(cfg.state_dir / "state.db")
            mem_bridge = _InMemoryBridge()
            gate = HumanGate()

            llm_create = mock.AsyncMock(return_value=_make_fake_llm_response("你好，内测朋友！"))
            llm_mock = types.SimpleNamespace(
                chat=types.SimpleNamespace(
                    completions=types.SimpleNamespace(create=llm_create)
                )
            )
            orch = HyperTextOrchestrator(
                config=cfg,
                store=store,
                bridge=mem_bridge,
                human_gate=gate,
                llm_client=llm_mock,
            )

            # Create a single-use invite code with custom limits (msg=2, browser=1)
            one_shot_code = store.create_invite_code(
                max_uses=1,
                daily_msg_limit=2,
                daily_browser_limit=1,
                note="single-use",
            )
            self.assertRegex(one_shot_code, r"^HT-[A-Z0-9]{6}$")

            # 1. Unknown sender sends plain greeting -> gets 1 polite invite prompt
            await orch.handle_inbound_message(
                _mk_inbound(1, member1, "你好呀")
            )
            self.assertEqual(len(mem_bridge.sent), 1)
            self.assertTrue(mem_bridge.sent[-1][1].startswith("👋 你好，当前 AI 助理处于邀请内测阶段"))

            # 2. Second unauthenticated message within 24h -> 0 replies (anti-spam!)
            await orch.handle_inbound_message(
                _mk_inbound(2, member1, "在不在？")
            )
            self.assertEqual(len(mem_bridge.sent), 1)

            # 3. Redeem single-use invite code -> activated + welcome message
            await orch.handle_inbound_message(
                _mk_inbound(3, member1, f"我的邀请码是 {one_shot_code}")
            )
            self.assertTrue(store.is_authorized_user(member1))
            self.assertTrue(mem_bridge.sent[-1][1].startswith("🎉 邀请码激活成功！"))

            # 4. Second user tries exhausted one-shot code -> fails; activates via HT-TEAM99 static seed code
            ok_exhausted, _ = store.redeem_invite_code(member2, one_shot_code)
            self.assertFalse(ok_exhausted)
            ok_static, _ = store.redeem_invite_code(member2, "/activate HT-TEAM99")
            self.assertTrue(ok_static)
            self.assertTrue(store.is_authorized_user(member2))

            # 4b. Legacy INST-* invite codes (INST-2IZ3MG & INST-TEAM99) redeem 100% validly!
            legacy_user_a = "+14155550901"
            legacy_user_b = "+14155550902"
            store.create_invite_code(code="INST-2IZ3MG", max_uses=1, note="legacy_db_code")
            ok_leg_a, used_a = store.redeem_invite_code(
                legacy_user_a, "你好，我的旧版邀请码是 INST-2IZ3MG 请激活"
            )
            self.assertTrue(ok_leg_a)
            self.assertEqual(used_a, "INST-2IZ3MG")
            self.assertTrue(store.is_authorized_user(legacy_user_a))

            ok_leg_b, used_b = store.redeem_invite_code(
                legacy_user_b, "/activate INST-TEAM99"
            )
            self.assertTrue(ok_leg_b)
            self.assertEqual(used_b, "INST-TEAM99")
            self.assertTrue(store.is_authorized_user(legacy_user_b))

            # 5. Activated external member chats WITHOUT '/ai' prefix; hits daily_msg_limit=2 on 3rd turn
            await orch.handle_inbound_message(
                _mk_inbound(4, member1, "第一条正式对话")
            )
            await orch.handle_inbound_message(
                _mk_inbound(5, member1, "第二条正式对话")
            )
            self.assertEqual(llm_create.call_count, 2)

            await orch.handle_inbound_message(
                _mk_inbound(6, member1, "第三条超额对话")
            )
            # LLM was NOT called a 3rd time; quota notice sent
            self.assertEqual(llm_create.call_count, 2)
            self.assertIn("⚠️", mem_bridge.sent[-1][1])

            # 6. Browser quota enforcement (limit=1 for member1, unlimited for admin)
            b_ok1, _ = store.check_and_increment_quota(member1, "browser", is_admin=False)
            b_ok2, _ = store.check_and_increment_quota(member1, "browser", is_admin=False)
            self.assertTrue(b_ok1)
            self.assertFalse(b_ok2)

            for _ in range(5):
                adm_ok, _ = store.check_and_increment_quota(admin, "browser", is_admin=True)
                self.assertTrue(adm_ok)

            store.close()

    def test_cli_create_invite_list_users_and_doctor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            env_file = tmp_path / "test.env"
            state_dir = tmp_path / "state"
            env_file.write_text(
                "\n".join(
                    [
                        "PROVIDER_MODE=deepseek",
                        "DEEPSEEK_MODEL=deepseek-flash",
                        f"STATE_DIR={state_dir}",
                        "ALLOWED_SENDERS=+14155550100",
                        "ALLOW_SELF_CHAT=true",
                        "SELF_CHAT_PREFIX=/ai",
                        "ENABLE_INVITE_CODES=true",
                        "BROWSER_MAX_CONCURRENCY=4",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            buf_create = io.StringIO()
            with contextlib.redirect_stdout(buf_create):
                rc1 = cli_main(
                    [
                        "--env-file",
                        str(env_file),
                        "--create-invite",
                        "--invite-code",
                        "HT-CLI2026",
                        "--invite-uses",
                        "3",
                        "--invite-note",
                        "QA Friends",
                    ]
                )
            self.assertEqual(rc1, 0)
            self.assertIn("HT-CLI2026", buf_create.getvalue())

            buf_list = io.StringIO()
            with contextlib.redirect_stdout(buf_list):
                rc2 = cli_main(["--env-file", str(env_file), "--list-users"])
            self.assertEqual(rc2, 0)
            self.assertIn("HT-CLI2026", buf_list.getvalue())

            buf_doc = io.StringIO()
            with contextlib.redirect_stdout(buf_doc):
                rc3 = cli_main(["--env-file", str(env_file), "--doctor"])
            self.assertEqual(rc3, 0)
            doc_out = buf_doc.getvalue()
            self.assertIn("ALLOW_SELF_CHAT", doc_out)
            self.assertIn("ENABLE_INVITE_CODES", doc_out)
            self.assertIn("BROWSER_MAX_CONCURRENCY", doc_out)

    def test_static_repo_audit_zero_personal_paths_env_and_deprecated_apis(self) -> None:
        self.assertEqual(__version__, "0.6.4")
        self.assertIs(InstinctOrchestrator, HyperTextOrchestrator)
        repo_root = pathlib.Path(__file__).resolve().parent.parent

        # 1. Repo root MUST ignore .env & local plist and NEVER track them in Git
        gitignore_text = (repo_root / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".env", gitignore_text)
        self.assertIn("deploy/*.local.plist", gitignore_text)
        if (repo_root / ".git").exists():
            import subprocess

            tracked = subprocess.run(
                ["git", "ls-files", ".env", "deploy/com.plotloop.hypertext-agent.local.plist"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(tracked.stdout.strip(), "", ".env or local plist must NEVER be tracked in Git!")
        self.assertTrue((repo_root / ".env.example").exists())
        self.assertFalse(
            (repo_root / "deploy" / "com.plotloop.hypertext-agent.plist").exists(),
            "Hardcoded plist must NOT exist; use .plist.example template",
        )
        plist_example = repo_root / "deploy" / "com.plotloop.hypertext-agent.plist.example"
        self.assertTrue(plist_example.exists())
        plist_text = plist_example.read_text(encoding="utf-8")
        self.assertIn("__PROJECT_ROOT__", plist_text)
        self.assertIn("__HOME_DIR__", plist_text)

        # 2. Zero personal home directory paths anywhere in tracked/distributable repo files
        forbidden_home = "/" + "Users/" + "r" + "k"
        skip_dirs = {
            ".git",
            "__pycache__",
            ".pytest_cache",
            ".venv",
            ".ruff_cache",
            ".hypertext",
            ".instinct_mac",
            "logo-handoff-gpt",
            "logo-candidates",
        }
        scanned_files = 0

        for path in repo_root.rglob("*"):
            if any(part in skip_dirs for part in path.parts) or not path.is_file():
                continue
            if (
                path.name in {".env", ".DS_Store"}
                or (path.name.startswith(".env.") and path.name != ".env.example")
                or path.name.endswith((".local.plist", ".log", ".save", ".swp", ".bak", ".zip"))
            ):
                continue
            raw = path.read_bytes()
            text = raw.decode("utf-8", errors="ignore")
            scanned_files += 1
            self.assertFalse(
                forbidden_home in text,
                f"Found hardcoded personal path in {path.relative_to(repo_root)}",
            )

        self.assertGreaterEqual(scanned_files, 20)

        # 3. Zero deprecated DeepSeek model names or removed Browser-Use 0.x APIs in Python code
        banned_tokens = [
            "deepseek-" + "chat",
            "deepseek-" + "reasoner",
            "Chat" + "DeepSeek",
            "Browser" + "Config",
            "Action" + "Result",
        ]
        zero_arg_browser_re = re.compile(r"(?<!\w)Browser\(\s*\)")

        py_files = [repo_root / "hypertext_agent.py"] + list(
            (repo_root / "hypertext").glob("*.py")
        )
        for py_path in py_files:
            src = py_path.read_text(encoding="utf-8")
            for tok in banned_tokens:
                self.assertFalse(
                    tok in src,
                    f"Banned deprecated identifier {tok!r} found in {py_path.name}",
                )
            self.assertIsNone(
                zero_arg_browser_re.search(src),
                f"Zero-arg Browser() call found in {py_path.name}",
            )


if __name__ == "__main__":
    unittest.main()
