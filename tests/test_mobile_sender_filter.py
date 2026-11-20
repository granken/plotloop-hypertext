"""Comprehensive tests for HyperText v0.6.4 real-mobile sender validation, iMessage-only ingress/egress guards, and junk invite-prompt cleanup."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import signal
import sqlite3
import tempfile
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

from hypertext import (
    AgentConfig,
    HumanGate,
    HyperTextOrchestrator,
    InboundMessage,
    MacIMessageBridge,
    MessageBridge,
    MessageDeliveryError,
    StateStore,
    is_real_mobile_sender,
    normalize_sender,
    run_allow_user,
)
from hypertext.cli import run_imessage_daemon
from hypertext.daemon_control import STOP_REQUEST


def _init_full_chat_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE handle (
            ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL,
            service TEXT
        );
        CREATE TABLE chat (
            ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
            guid TEXT,
            chat_identifier TEXT,
            service_name TEXT,
            style INTEGER DEFAULT 45
        );
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
            guid TEXT,
            text TEXT,
            attributedBody BLOB,
            handle_id INTEGER DEFAULT 0,
            service TEXT,
            is_from_me INTEGER DEFAULT 0,
            date INTEGER DEFAULT 0,
            cache_has_attachments INTEGER DEFAULT 0,
            associated_message_type INTEGER DEFAULT 0
        );
        CREATE TABLE chat_message_join (
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


class _FakeProc:
    def __init__(self, returncode: int = 0, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return (b"", self._stderr)


class TestMobileSenderFilterV064(unittest.IsolatedAsyncioTestCase):
    def test_is_real_mobile_sender_and_normalize_invariant(self) -> None:
        valid_inputs = [
            "+8613800138000",
            "13800138000",
            "+86 138-0013-8000",
            "+86 (139) 0000-1111",
            "137 0000 2222",
            "+85251234567",
            "+85246234567",
            "+85270123456",
            "+85271012345",
            "+85298765432",
            "+85366123456",
            "+886912345678",
            "+14155550901",
            "+1 (415) 555-0902",
            "+15305550901",
            "+6580212345",
            "+6581234567",
            "+6598765432",
            "+447911123456",
            "+447624123456",
            "+819012345678",
            "+817012345678",
        ]
        rejected_inputs = [
            "",
            "   ",
            "\u202a\u202c",
            "+86.138.0013.8000",
            "10694727302053",
            "+8610694727302053",
            "+8617400001234",
            "17400001234",
            "+8614100001234",
            "14100001234",
            "106878632488111861",
            "10686582206101@botplatform.rcs.wo.cn",
            "10010",
            "10086",
            "10000",
            "12306",
            "12123",
            "110",
            "95555",
            "95588",
            "96110",
            "95555(filtered)",
            "+8613800138000(smsft)",
            "13800138000(smsft)",
            "4008108888",
            "8008208888",
            "+864008108888",
            "+868008208888",
            "01088888888",
            "+861088888888",
            "+862188888888",
            "+861380013800",
            "+86138001380001",
            "+85221234567",
            "+85271001234",
            "+85299912345",
            "+85328123456",
            "+85361234567",
            "+886223456789",
            "+886991234567",
            "+18005550199",
            "+18225550199",
            "+18335550199",
            "+18885550199",
            "+19005550199",
            "+19115550199",
            "+14565550901",
            "+15005550901",
            "+15215550901",
            "+15325550901",
            "+15335550901",
            "+15355550901",
            "+15385550901",
            "+15885550901",
            "+16005550901",
            "+17005550901",
            "+17105550901",
            "+12905550901",
            "+13705550901",
            "+14159110901",
            "+14155550199",
            "+442079460958",
            "+447012345678",
            "+447600123456",
            "+81312345678",
            "+818001234567",
            "+6561234567",
            "+6580012345",
            "+6424012345",
            "+493012345678",
            "+498001234567",
            "+33142685500",
            "+33800123456",
            "+80012345678",
            "+80812345678",
            "+870771234567",
            "+881612345678",
            "+88216123456",
            "+883510012345",
            "+979123456789",
            "+995555123456",
            "+01234567890",
            "+8695555",
            "chat458291048",
            "iMessage;+;chat458291048",
            "SMS;-;+8613800138000",
            "urn:biz:apple-support",
            "SKP",
            "Apple",
            "attacker@example.com",
            "１３８００１３８０００",
        ]

        for item in valid_inputs:
            self.assertTrue(is_real_mobile_sender(item), f"Should accept {item!r}")
            norm = normalize_sender(item)
            self.assertTrue(
                is_real_mobile_sender(norm),
                f"Normalized {item!r} -> {norm!r} must also be accepted",
            )

        for item in rejected_inputs:
            self.assertFalse(is_real_mobile_sender(item), f"Should reject {item!r}")
            norm = normalize_sender(item)
            self.assertFalse(
                is_real_mobile_sender(norm),
                f"Normalized {item!r} -> {norm!r} must also be rejected",
            )

        self.assertEqual(
            normalize_sender("+86 (138) 0013-8000"), "+8613800138000"
        )
        self.assertEqual(AgentConfig().imessage_only_service, True)
        self.assertEqual(
            AgentConfig.from_env(environ={"IMESSAGE_ONLY_SERVICE": "false"}).imessage_only_service,
            False,
        )

    def test_poll_new_messages_drops_sms_rcs_service_codes_and_groups_while_preserving_imessage_and_self_chat(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            chat_db = tmp_path / "chat.db"
            _init_full_chat_db(chat_db)

            cfg = AgentConfig(
                state_dir=tmp_path / "state",
                chat_db_path=chat_db,
                allowed_senders={"owner@icloud.com", "+8613800138000"},
                allow_self_chat=True,
                self_chat_prefix="/ai",
                enable_invite_codes=True,
            )
            store = StateStore(cfg.sqlite_db_path)
            bridge = MacIMessageBridge(config=cfg, store=store)
            self.assertEqual(bridge.align_cursor_to_latest(), 0)

            conn = sqlite3.connect(str(chat_db))
            conn.executemany(
                "INSERT INTO handle(ROWID, id, service) VALUES (?, ?, ?)",
                [
                    (1, "owner@icloud.com", "iMessage"),
                    (2, "10694727302053", "SMS"),
                    (3, "+8613900001111", "SMS"),
                    (4, "10686582206101@botplatform.rcs.wo.cn", "RCS"),
                    (5, "106878632488111861", "iMessage"),
                    (6, "95555(filtered)", "iMessage"),
                    (7, "attacker@example.com", "iMessage"),
                    (8, "+8613900002222", "iMessage"),
                    (9, "13900003333", "iMessage"),
                    (10, "+8613900004444", ""),
                ],
            )
            conn.executemany(
                "INSERT INTO chat(ROWID, guid, chat_identifier, service_name, style) VALUES (?, ?, ?, ?, ?)",
                [
                    (1, "iMessage;-;owner@icloud.com", "owner@icloud.com", "iMessage", 45),
                    (2, "SMS;-;10694727302053", "10694727302053", "SMS", 45),
                    (3, "SMS;-;+8613900001111", "+8613900001111", "SMS", 45),
                    (
                        4,
                        "RCS;-;10686582206101@botplatform.rcs.wo.cn",
                        "10686582206101@botplatform.rcs.wo.cn",
                        "RCS",
                        45,
                    ),
                    (5, "iMessage;-;106878632488111861", "106878632488111861", "iMessage", 45),
                    (6, "iMessage;-;95555(filtered)", "95555(filtered)", "iMessage", 45),
                    (7, "iMessage;-;attacker@example.com", "attacker@example.com", "iMessage", 45),
                    (8, "iMessage;+;chat458291048", "chat458291048", "iMessage", 43),
                    (9, "iMessage;-;+8613900002222", "+8613900002222", "iMessage", 45),
                    (10, "any;-;13900003333", "13900003333", "iMessage", 45),
                    (11, "", "chat99887766", "iMessage", 45),
                    (12, "any;-;+8613900004444", "+8613900004444", "", 45),
                    (13, "", "+8613900004444", "", 45),
                    (14, "any;-;13900003333", "13900003333", "SMS", 45),
                ],
            )
            conn.executemany(
                "INSERT INTO message(ROWID, guid, text, handle_id, service, is_from_me) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    # 1. Carrier 1069 SMS
                    (1, "drop-sms-1069", "【中国联通】话费账单提醒", 2, "SMS", 0),
                    # 2. Real mobile +86 number arriving over SMS (must STILL be dropped!)
                    (2, "drop-sms-mobile", "普通短信发来的内容", 3, "SMS", 0),
                    # 3. Carrier RCS bot
                    (3, "drop-rcs-bot", "RCS 营业厅卡片", 4, "RCS", 0),
                    # 4. 1068 SP code even if labelled iMessage
                    (4, "drop-im-1068", "验证码 883920", 5, "iMessage", 0),
                    # 5. Filtered spam handle
                    (5, "drop-im-filtered", "银行动账通知", 6, "iMessage", 0),
                    # 6. Non-admin external email over iMessage
                    (6, "drop-im-email", "hello from random email", 7, "iMessage", 0),
                    # 7. Group chat (;+; and style=43) even from real mobile participant
                    (7, "drop-group-guid", "群里有人吗", 8, "iMessage", 0),
                    # 8. Group chat even from admin Apple ID
                    (8, "drop-group-admin", "/ai 群聊里误触发", 1, "iMessage", 1),
                    # 9. Group chat detected solely by chat_identifier=chat\d+
                    (9, "drop-group-ident", "无GUID群聊", 8, "iMessage", 0),
                    # 10. Real +86 iMessage external sender
                    (10, "ok-im-plus86", "你好，我想体验助理", 8, "iMessage", 0),
                    # 11. Real bare 11-digit Mainland mobile iMessage with Sonoma `any;-;` chat.guid
                    (11, "ok-im-bare11", "HT-INVITE", 9, "iMessage", 0),
                    # 12. Owner Apple ID email self-chat
                    (12, "ok-admin-self", "/ai 总结今天日程", 1, "iMessage", 1),
                    # 13. Unified `any;-;` chat with empty m.service / h.service / c.service_name -> dropped!
                    (13, "drop-any-no-svc", "缺少正证据的any会话", 10, "", 0),
                    # 14. Empty guid & empty service columns on service-bearing schema -> dropped!
                    (14, "drop-empty-no-svc", "缺少正证据的空GUID会话", 10, "", 0),
                    # 15. SMS arriving inside a unified chat whose chat.service_name is 'iMessage' -> dropped!
                    (15, "drop-sms-in-unified", "统一会话里的普通短信", 9, "SMS", 0),
                    # 16. iMessage arriving in a unified chat whose stale chat.service_name was 'SMS' -> admitted!
                    (16, "ok-im-in-stale-sms-chat", "切回iMessage后的消息", 9, "iMessage", 0),
                ],
            )
            conn.executemany(
                "INSERT INTO chat_message_join(chat_id, message_id) VALUES (?, ?)",
                [
                    (2, 1),
                    (3, 2),
                    (4, 3),
                    (5, 4),
                    (6, 5),
                    (7, 6),
                    (8, 7),
                    (8, 8),
                    (11, 9),
                    (9, 10),
                    (10, 11),
                    (1, 12),
                    (12, 13),
                    (13, 14),
                    (10, 15),
                    (14, 16),
                ],
            )
            conn.commit()
            conn.close()

            polled = bridge.poll_new_messages()
            self.assertEqual(
                [m.guid for m in polled],
                [
                    "ok-im-plus86",
                    "ok-im-bare11",
                    "ok-admin-self",
                    "ok-im-in-stale-sms-chat",
                ],
            )
            self.assertEqual(polled[0].reply_target, "iMessage;-;+8613900002222")
            # `any;-;...` must NOT be passed as chat_guid reply_target when imessage_only_service=True
            self.assertEqual(polled[1].reply_target, "13900003333")
            self.assertEqual(polled[2].reply_target, "iMessage;-;owner@icloud.com")
            self.assertEqual(polled[2].text, "总结今天日程")
            store.close()

    async def test_egress_guard_blocks_service_numbers_sms_guids_and_groups_without_osascript(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            chat_db = tmp_path / "chat.db"
            _init_full_chat_db(chat_db)

            cfg = AgentConfig(
                state_dir=tmp_path / "state",
                chat_db_path=chat_db,
                allowed_senders={"+8613800138000", "owner@icloud.com"},
                self_chat_reply_to="self-alias@icloud.com",
            )
            store = StateStore(cfg.sqlite_db_path)
            bridge = MacIMessageBridge(config=cfg, store=store)

            attachment = tmp_path / "note.txt"
            attachment.write_text("hello", encoding="utf-8")

            blocked_targets = [
                "",
                "   ",
                "10694727302053",
                "95555",
                "95555(filtered)",
                "10686582206101@botplatform.rcs.wo.cn",
                "attacker@example.com",
                "SMS;-;+8613800138000",
                "RCS;-;+8613900001111",
                "any;-;+8613800138000",
                "iMessage;+;chat458291048",
                "chat458291048",
                "iMessage;-;chat458291048",
            ]

            with (
                mock.patch("asyncio.create_subprocess_exec") as exec_mock,
                mock.patch.object(bridge, "_stage_to_imessage_outbox") as stage_mock,
            ):
                for bad in blocked_targets:
                    with self.assertRaises(MessageDeliveryError, msg=f"send_text should block {bad!r}"):
                        await bridge.send_text(bad, "测试拦截")
                    with self.assertRaises(
                        MessageDeliveryError, msg=f"send_attachment should block {bad!r}"
                    ):
                        await bridge.send_attachment(bad, attachment)

                self.assertEqual(exec_mock.call_count, 0)
                self.assertEqual(stage_mock.call_count, 0)
                self.assertEqual(bridge._recent_outbound, {})

            # Verify valid recipients (admin email, pinned self_chat_reply_to, and real mobile) succeed
            calls: list[tuple[str, ...]] = []

            async def ok_exec(*args: str, **kwargs: object) -> _FakeProc:
                calls.append(tuple(str(a) for a in args))
                return _FakeProc(0)

            with mock.patch("asyncio.create_subprocess_exec", side_effect=ok_exec):
                await bridge.send_text("owner@icloud.com", "自聊正常")
                await bridge.send_text("self-alias@icloud.com", "指定回复地址正常")
                await bridge.send_text("\u202a+8613900001111\u202c", "真手机号正常")
                await bridge.send_text("iMessage;-;+8613900001111", "iMessage单聊GUID正常")

            self.assertEqual(len(calls), 4)
            self.assertEqual(calls[2][3], "+8613900001111")
            store.close()

    async def test_store_startup_purge_and_orchestrator_defense_in_depth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "state" / "state.sqlite3"
            db_path.parent.mkdir(parents=True, exist_ok=True)

            # Pre-seed historical junk invite_prompt_ts:* keys and a legacy non-mobile authorized_users row
            raw_conn = sqlite3.connect(str(db_path))
            raw_conn.executescript(
                """
                CREATE TABLE kv_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE authorized_users (
                    sender TEXT PRIMARY KEY,
                    role TEXT NOT NULL DEFAULT 'member',
                    activated_via TEXT,
                    daily_msg_limit INTEGER NOT NULL DEFAULT 50,
                    daily_browser_limit INTEGER NOT NULL DEFAULT 10,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL
                );
                """
            )
            raw_conn.executemany(
                "INSERT INTO kv_meta(key, value, updated_at) VALUES (?, '1700000000', 1700000000)",
                [
                    ("last_imessage_rowid",),
                    ("invite_prompt_ts:10694727302053",),
                    ("invite_prompt_ts:10010",),
                    ("invite_prompt_ts:95555",),
                    ("invite_prompt_ts:10686582206101@botplatform.rcs.wo.cn",),
                    ("invite_prompt_ts:+8613900001111",),
                ],
            )
            raw_conn.execute(
                "INSERT INTO authorized_users(sender, status, created_at) VALUES ('10694727302053', 'active', '2026-01-01T00:00:00')"
            )
            raw_conn.commit()
            raw_conn.close()

            store = StateStore(db_path)
            # Plain StateStore(db_path) preserves non-mobile invite_prompt_ts keys until MacIMessageBridge attaches
            self.assertEqual(store.get_meta("invite_prompt_ts:10694727302053"), "1700000000")
            chat_db = tmp_path / "orch_chat.db"
            _init_full_chat_db(chat_db)
            MacIMessageBridge(
                chat_db_path=chat_db,
                store=store,
                allowed_senders={"owner@icloud.com"},
            )
            # Junk invite_prompt_ts keys must be purged automatically when MacIMessageBridge initializes
            self.assertIsNone(store.get_meta("invite_prompt_ts:10694727302053"))
            self.assertIsNone(store.get_meta("invite_prompt_ts:10010"))
            self.assertIsNone(store.get_meta("invite_prompt_ts:95555"))
            self.assertIsNone(
                store.get_meta("invite_prompt_ts:10686582206101@botplatform.rcs.wo.cn")
            )
            # Valid mobile cooldown and cursor meta must remain intact
            self.assertEqual(store.get_meta("invite_prompt_ts:+8613900001111"), "1700000000")
            self.assertEqual(store.get_meta("last_imessage_rowid"), "1700000000")

            # Legacy non-mobile authorized_users row must NOT be treated as authorized
            self.assertFalse(store.is_authorized_user("10694727302053"))

            # Invite code cannot be consumed by service numbers / emails
            code = store.create_invite_code(code="HT-ONESHOT", max_uses=1)
            self.assertEqual(
                store.redeem_invite_code("10694727302053", code),
                (False, "non_mobile_sender"),
            )
            self.assertEqual(
                store.redeem_invite_code("attacker@example.com", f"/activate {code}"),
                (False, "non_mobile_sender"),
            )
            self.assertEqual(store.list_invite_codes()[0]["used_count"], 0)

            # Non-mobile invite prompt check returns False without polluting kv_meta
            self.assertFalse(store.should_send_invite_prompt("10694727302053"))
            self.assertIsNone(store.get_meta("invite_prompt_ts:10694727302053"))

            # allow_user & CLI run_allow_user reject non-mobile handles
            with self.assertRaises(ValueError):
                store.allow_user("10694727302053")
            with self.assertRaises(ValueError):
                store.allow_user("friend@example.com")

            cfg = AgentConfig(
                state_dir=tmp_path / "state",
                allowed_senders={"owner@icloud.com"},
                enable_invite_codes=True,
                deepseek_api_key="sk-valid-test-key-v064",
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc_bad = run_allow_user(cfg, "friend@example.com")
            self.assertEqual(rc_bad, 1)
            self.assertIn("ALLOWED_SENDERS", buf.getvalue())

            # Real mobile still redeems the unconsumed single-use invite code cleanly
            ok_real, used_code = store.redeem_invite_code("+86 139-0000-5555", code)
            self.assertTrue(ok_real)
            self.assertEqual(used_code, code)
            self.assertTrue(store.is_authorized_user("+8613900005555"))

            # Orchestrator defense-in-depth on MacIMessageBridge:
            # handle_inbound_message, notify_interrupted_imessage, process_due_tasks_once
            cfg.chat_db_path = chat_db
            im_outbox: list[tuple[str, str]] = []

            async def record_send(recipient: str, text: str) -> None:
                im_outbox.append((recipient, text))

            bridge = MacIMessageBridge(config=cfg, store=store)
            bridge.send_text = record_send  # type: ignore[method-assign]
            gate = HumanGate()
            orch = HyperTextOrchestrator(
                config=cfg, store=store, bridge=bridge, human_gate=gate
            )

            for junk_sender in (
                "10694727302053",
                "95555",
                "10686582206101@botplatform.rcs.wo.cn",
                "attacker@example.com",
                "+493012345678",
                "+80012345678",
            ):
                res = await orch.handle_inbound_message(
                    InboundMessage(
                        rowid=1,
                        guid="junk",
                        sender=junk_sender,
                        text=code,
                        timestamp_unix=time.time(),
                        service="iMessage",
                    )
                )
                self.assertIsNone(res)
                await orch.notify_interrupted_imessage(
                    InboundMessage(
                        rowid=2,
                        guid="junk-int",
                        sender=junk_sender,
                        text="interrupted",
                        timestamp_unix=time.time(),
                        service="iMessage",
                    )
                )
            self.assertEqual(im_outbox, [])

            # Stale SMS/group reply_target or service="SMS" on InboundMessage is also dropped
            sms_tagged = InboundMessage(
                rowid=3,
                guid="stale-sms",
                sender="+8613900005555",
                text="你好",
                timestamp_unix=time.time(),
                reply_target="SMS;-;+8613900005555",
                service="SMS",
            )
            self.assertIsNone(await orch.handle_inbound_message(sms_tagged))
            await orch.notify_interrupted_imessage(sms_tagged)
            self.assertEqual(im_outbox, [])

            # Non-mobile scheduled task is cancelled without executing on MacIMessageBridge
            now = time.time()
            bad_tid = store.add_scheduled_task(
                "10694727302053", "direct_notify", "不应发送的提醒", now - 1, interval_seconds=60
            )
            executed = await orch.process_due_tasks_once(now_ts=now)
            self.assertEqual(executed, 0)
            self.assertEqual(im_outbox, [])
            self.assertEqual(store.list_scheduled_tasks(only_enabled=True), [])
            self.assertFalse(store.cancel_scheduled_task(bad_tid))

            # Admin Apple ID email interruption notice still works and sanitizes `any;-;`
            admin_int = InboundMessage(
                rowid=4,
                guid="adm-int",
                sender="owner@icloud.com",
                text="/ai test",
                timestamp_unix=now,
                reply_target="any;-;owner@icloud.com",
                service="iMessage",
            )
            await orch.notify_interrupted_imessage(admin_int)
            self.assertEqual(len(im_outbox), 1)
            self.assertEqual(im_outbox[0][0], "owner@icloud.com")
            store.close()

    async def test_queued_inbox_replay_revalidates_legacy_sms_and_group_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            chat_db = tmp_path / "chat.db"
            _init_full_chat_db(chat_db)

            conn = sqlite3.connect(str(chat_db))
            conn.executemany(
                "INSERT INTO handle(ROWID, id, service) VALUES (?, ?, ?)",
                [
                    (1, "owner@icloud.com", "iMessage"),
                    (2, "+8613900001111", "SMS"),
                    (3, "+8613900002222", "iMessage"),
                ],
            )
            conn.executemany(
                "INSERT INTO chat(ROWID, guid, chat_identifier, service_name, style) VALUES (?, ?, ?, ?, ?)",
                [
                    (1, "iMessage;+;chat458291048", "chat458291048", "iMessage", 43),
                    (2, "SMS;-;+8613900001111", "+8613900001111", "SMS", 45),
                    (3, "iMessage;-;+8613900002222", "+8613900002222", "iMessage", 45),
                ],
            )
            conn.executemany(
                "INSERT INTO message(ROWID, guid, text, handle_id, service, is_from_me) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    # row 1: Admin sent `/ai` inside a group chat before upgrade (SELF_CHAT_REPLY_TO hid `;+;`)
                    (1, "leg-grp", "/ai 群聊旧队列", 1, "iMessage", 1),
                    # row 2: Real mobile sent SMS before upgrade
                    (2, "leg-sms", "普通短信旧队列", 2, "SMS", 0),
                    # row 3: Valid real mobile iMessage before upgrade
                    (3, "leg-ok", "/ping", 3, "iMessage", 0),
                ],
            )
            conn.executemany(
                "INSERT INTO chat_message_join(chat_id, message_id) VALUES (?, ?)",
                [(1, 1), (2, 2), (3, 3)],
            )
            conn.commit()
            conn.close()

            cfg = AgentConfig(
                state_dir=tmp_path / "state",
                chat_db_path=chat_db,
                allowed_senders={"owner@icloud.com"},
                allow_self_chat=True,
                self_chat_reply_to="owner@icloud.com",
                enable_invite_codes=True,
                poll_interval_sec=0.05,
                deepseek_api_key="sk-valid-test-key-v064",
            )
            with contextlib.closing(StateStore(cfg.sqlite_db_path)) as store:
                store.allow_user("+8613900001111", note="sms-contact")
                store.allow_user("+8613900002222", note="im-member")
                # Legacy pre-v0.6.4 JSON payloads (no `service` or `is_group` keys!)
                legacy_rows = [
                    (
                        1,
                        json.dumps(
                            {
                                "rowid": 1,
                                "guid": "leg-grp",
                                "sender": "owner@icloud.com",
                                "text": "群聊旧队列",
                                "timestamp_unix": 1700000001.0,
                                "is_from_me": 1,
                                "reply_target": "owner@icloud.com",
                            }
                        ),
                    ),
                    (
                        2,
                        json.dumps(
                            {
                                "rowid": 2,
                                "guid": "leg-sms",
                                "sender": "+8613900001111",
                                "text": "普通短信旧队列",
                                "timestamp_unix": 1700000002.0,
                                "is_from_me": 0,
                                "reply_target": "+8613900001111",
                            }
                        ),
                    ),
                    (
                        3,
                        json.dumps(
                            {
                                "rowid": 3,
                                "guid": "leg-ok",
                                "sender": "+8613900002222",
                                "text": "/ping",
                                "timestamp_unix": 1700000003.0,
                                "is_from_me": 0,
                                "reply_target": "iMessage;-;+8613900002222",
                            }
                        ),
                    ),
                    # row 99: Legacy pre-v0.6.4 row deleted from chat.db (no transport evidence) -> dropped!
                    (
                        99,
                        json.dumps(
                            {
                                "rowid": 99,
                                "guid": "leg-deleted",
                                "sender": "+8613900001111",
                                "text": "已从chat.db删除的旧短信",
                                "timestamp_unix": 1700000004.0,
                                "is_from_me": 0,
                                "reply_target": "+8613900001111",
                            }
                        ),
                    ),
                ]
                store.record_imessage_batch(MacIMessageBridge.CURSOR_META_KEY, 99, legacy_rows)
                store.start_imessage(2)  # Even interrupted SMS must NOT send interruption notice!

            handled_rowids: list[int] = []
            interrupted_rowids: list[int] = []
            orig_revalidate = MacIMessageBridge.revalidate_queued_message
            transient_raised = False

            def flaky_revalidate(
                self_b: MacIMessageBridge, msg: InboundMessage, *, check_db: bool = False
            ) -> bool:
                nonlocal transient_raised
                if msg.rowid == 3 and not transient_raised:
                    transient_raised = True
                    raise sqlite3.OperationalError("database is locked")
                return orig_revalidate(self_b, msg, check_db=check_db)

            async def fake_handle(_orch: HyperTextOrchestrator, msg: InboundMessage) -> str | None:
                handled_rowids.append(msg.rowid)
                (cfg.state_dir / STOP_REQUEST).write_text("stop-after-ok", encoding="ascii")
                return "ok"

            async def fake_interrupt(_orch: HyperTextOrchestrator, msg: InboundMessage) -> None:
                interrupted_rowids.append(msg.rowid)

            prev_hup = signal.getsignal(signal.SIGHUP)
            try:
                with (
                    mock.patch.object(
                        MacIMessageBridge, "revalidate_queued_message", flaky_revalidate
                    ),
                    mock.patch.object(HyperTextOrchestrator, "handle_inbound_message", fake_handle),
                    mock.patch.object(
                        HyperTextOrchestrator, "notify_interrupted_imessage", fake_interrupt
                    ),
                ):
                    await asyncio.wait_for(run_imessage_daemon(cfg), timeout=3.0)
            finally:
                signal.signal(signal.SIGHUP, prev_hup)

            self.assertTrue(transient_raised)
            self.assertEqual(handled_rowids, [3])
            self.assertEqual(interrupted_rowids, [])
            with contextlib.closing(StateStore(cfg.sqlite_db_path)) as store:
                self.assertEqual(store.pending_imessages(), [])

    async def test_custom_non_imessage_bridge_preserves_beta_access_and_bridge_transport_override(
        self,
    ) -> None:
        class _CustomFeishuBridge(MessageBridge):
            def __init__(self) -> None:
                self.sent: list[tuple[str, str]] = []

            async def send_text(self, recipient: str, text: str) -> None:
                self.sent.append((recipient, text))

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg = AgentConfig(
                state_dir=tmp_path / "feishu_state",
                allowed_senders={"admin_open_id"},
                enable_invite_codes=True,
                deepseek_api_key="sk-valid-test-key-v064",
            )
            store = StateStore(cfg.sqlite_db_path, require_mobile_senders=False)
            feishu_bridge = _CustomFeishuBridge()
            self.assertFalse(feishu_bridge.requires_real_mobile_sender)
            orch = HyperTextOrchestrator(
                config=cfg, store=store, bridge=feishu_bridge, human_gate=HumanGate()
            )

            feishu_user = "feishu:ou_abc123"
            code = store.create_invite_code(code="HT-FEISHU", max_uses=1)

            # 1. Stranger on Feishu gets polite invite prompt once, and reopening plain StateStore(path) preserves 24h cooldown!
            p1 = await orch.handle_inbound_message(
                InboundMessage(1, "f1", feishu_user, "你好", time.time())
            )
            self.assertIsNotNone(p1)
            self.assertIn("邀请码", p1 or "")
            store.close()

            store = StateStore(cfg.sqlite_db_path)
            orch.store = store
            p2 = await orch.handle_inbound_message(
                InboundMessage(11, "f1-repeat", feishu_user, "再问一次", time.time())
            )
            self.assertIsNone(p2)

            # 2. Feishu opaque user ID redeems invite code and runs `/ping` (preserving `any;-;` custom reply_target!) + scheduled task
            r_act = await orch.handle_inbound_message(
                InboundMessage(2, "f2", feishu_user, code, time.time())
            )
            self.assertIsNotNone(r_act)
            self.assertIn("激活成功", r_act or "")
            self.assertTrue(store.is_authorized_user(feishu_user, require_mobile=False))

            r_ping = await orch.handle_inbound_message(
                InboundMessage(
                    3,
                    "f3",
                    feishu_user,
                    "/ping",
                    time.time(),
                    reply_target="any;-;custom-feishu-thread",
                )
            )
            self.assertIn("在线", r_ping or "")
            self.assertEqual(feishu_bridge.sent[-1][0], "any;-;custom-feishu-thread")

            now = time.time()
            store.add_scheduled_task(feishu_user, "direct_notify", "飞书提醒", now - 1)
            self.assertEqual(await orch.process_due_tasks_once(now_ts=now), 1)
            store.close()

            # Legacy pre-v0.6.4 iMessage row carrying mismatched `SMS;-;` chat GUID is accepted & sanitized!
            mismatched_chat_db = tmp_path / "mismatched_chat.db"
            _init_full_chat_db(mismatched_chat_db)
            with contextlib.closing(sqlite3.connect(mismatched_chat_db)) as raw_c, raw_c:
                raw_c.execute(
                    "INSERT INTO handle(ROWID, id, service) VALUES (1, '+8613800138000', 'iMessage')"
                )
                raw_c.execute(
                    "INSERT INTO chat(ROWID, guid, chat_identifier, service_name, style) "
                    "VALUES (1, 'SMS;-;+8613800138000', '+8613800138000', 'SMS', 45)"
                )
                raw_c.execute(
                    "INSERT INTO message(ROWID, guid, text, handle_id, service, is_from_me) "
                    "VALUES (1, 'leg-im-in-sms-chat', '/ping', 1, 'iMessage', 0)"
                )
                raw_c.execute("INSERT INTO chat_message_join(chat_id, message_id) VALUES (1, 1)")
            with contextlib.closing(StateStore(tmp_path / "mismatched_state.sqlite3")) as s_mis:
                s_mis.allow_user("+8613800138000")
                b_mis = MacIMessageBridge(mismatched_chat_db, s_mis)
                legacy_im = InboundMessage(
                    1,
                    "leg-im-in-sms-chat",
                    "+8613800138000",
                    "/ping",
                    now,
                    reply_target="SMS;-;+8613800138000",
                )
                self.assertTrue(b_mis.revalidate_queued_message(legacy_im))
                self.assertEqual(legacy_im.service, "iMessage")
                self.assertEqual(legacy_im.reply_target, "+8613800138000")

                # Rebuilt chat.db where ROWID 1 now has a different GUID must be rejected!
                mismatched_guid = InboundMessage(
                    1,
                    "different-older-guid",
                    "+8613800138000",
                    "/ping",
                    now,
                )
                self.assertFalse(b_mis.revalidate_queued_message(mismatched_guid))

                # Conflicting fallback metadata (empty m.service, iMessage handle, SMS chat) is rejected!
                self.assertEqual(
                    MacIMessageBridge._classify_row_transport(
                        "", "iMessage", "SMS", "SMS;-;+8613800138000"
                    ),
                    "conflict",
                )

                # Missing chat.db raises sqlite3.OperationalError in strict mode so cli.py retains queue!
                b_missing = MacIMessageBridge(tmp_path / "does_not_exist.db", s_mis)
                with self.assertRaises(sqlite3.OperationalError):
                    b_missing.revalidate_queued_message(
                        InboundMessage(1, "leg-im-in-sms-chat", "+8613800138000", "/ping", now)
                    )

            # Plain StateStore(path) remains transport-neutral by default for custom adapters!
            with contextlib.closing(StateStore(tmp_path / "neutral_adapter.sqlite3")) as s_neu:
                s_neu.allow_user("feishu:ou_direct_admin_cli")
                self.assertTrue(s_neu.is_authorized_user("feishu:ou_direct_admin_cli"))

            # 3. Finding 5: Bridge-level `imessage_only_service=False` overrides `cfg.imessage_only_service=True`
            chat_db = tmp_path / "override_chat.db"
            _init_full_chat_db(chat_db)
            cfg_im = AgentConfig(
                state_dir=tmp_path / "im_override",
                chat_db_path=chat_db,
                allowed_senders={"+8613800138000"},
                imessage_only_service=True,
                deepseek_api_key="sk-valid-test-key-v064",
            )
            store_im = StateStore(cfg_im.sqlite_db_path)
            sms_ok_bridge = MacIMessageBridge(
                config=cfg_im, store=store_im, imessage_only_service=False
            )
            delivered: list[tuple[str, str]] = []

            async def capture_send(recipient: str, text: str) -> None:
                delivered.append((recipient, text))

            sms_ok_bridge.send_text = capture_send  # type: ignore[method-assign]
            orch_im = HyperTextOrchestrator(
                config=cfg_im, store=store_im, bridge=sms_ok_bridge, human_gate=HumanGate()
            )
            sms_ping = InboundMessage(
                rowid=10,
                guid="sms-opt-in",
                sender="+8613800138000",
                text="/ping",
                timestamp_unix=now,
                reply_target="any;-;+8613800138000",
                service="SMS",
            )
            res_sms = await orch_im.handle_inbound_message(sms_ping)
            self.assertIn("在线", res_sms or "")
            # Opt-in SMS mode preserves unified `any;-;` direct-chat target!
            self.assertEqual(delivered[0][0], "any;-;+8613800138000")

            # Strict mode with `SELF_CHAT_REPLY_TO="any;-;self-alias@icloud.com"` normalizes to participant
            cfg_pinned = AgentConfig(
                state_dir=tmp_path / "im_pinned_any",
                chat_db_path=chat_db,
                allowed_senders={"+8613800138000"},
                allow_self_chat=True,
                self_chat_reply_to="any;-;self-alias@icloud.com",
                imessage_only_service=True,
                deepseek_api_key="sk-valid-test-key-v064",
            )
            pinned_bridge = MacIMessageBridge(config=cfg_pinned, store=store_im)
            pinned_calls: list[tuple[str, ...]] = []

            async def pinned_exec(*args: str, **kwargs: object) -> _FakeProc:
                pinned_calls.append(tuple(str(a) for a in args))
                return _FakeProc(0)

            orch_pinned = HyperTextOrchestrator(
                config=cfg_pinned,
                store=store_im,
                bridge=pinned_bridge,
                human_gate=HumanGate(),
            )
            with mock.patch("asyncio.create_subprocess_exec", side_effect=pinned_exec):
                res_pin = await orch_pinned.handle_inbound_message(
                    InboundMessage(
                        rowid=12,
                        guid="pin-self",
                        sender="+8613800138000",
                        text="/ping",
                        timestamp_unix=now,
                        is_from_me=1,
                        service="iMessage",
                    )
                )
                store_im.add_scheduled_task(
                    "+8613800138000", "direct_notify", "固定统一会话提醒", now - 1
                )
                self.assertEqual(await orch_pinned.process_due_tasks_once(now_ts=now), 1)
            self.assertIn("在线", res_pin or "")
            self.assertEqual(len(pinned_calls), 2)
            self.assertEqual(pinned_calls[0][3], "self-alias@icloud.com")
            self.assertEqual(pinned_calls[1][3], "self-alias@icloud.com")
            store_im.close()


if __name__ == "__main__":
    unittest.main()
