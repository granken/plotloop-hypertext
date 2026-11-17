"""Comprehensive unit tests for HyperText v0.5.1 Single-Exit Quiet-Tool UX & live iPhone Air + Mac mini bugfixes."""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from hypertext import (
    APPLESCRIPT_SEND_ATTACHMENT,
    APPLESCRIPT_SEND_IMESSAGE,
    BOT_ECHO_MARKER,
    AgentConfig,
    BrowserSandbox,
    CliBridge,
    HumanGate,
    HyperTextOrchestrator,
    InboundMessage,
    InstinctOrchestrator,
    MacIMessageBridge,
    StateStore,
    __version__,
    render_mobile_html_card,
    split_reply_and_standalone_links,
)
from hypertext.selftest import (
    _MockAsyncOpenAI,
    _MockChoice,
    _MockCompletion,
    _MockFunction,
    _MockMessage,
    _MockToolCall,
)


def _init_chat_db_with_guid(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE handle (
            ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL
        );
        CREATE TABLE chat (
            ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
            guid TEXT NOT NULL,
            chat_identifier TEXT NOT NULL
        );
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
            guid TEXT,
            text TEXT,
            attributedBody BLOB,
            handle_id INTEGER,
            is_from_me INTEGER DEFAULT 0,
            date INTEGER DEFAULT 750000000000000000,
            associated_message_type INTEGER DEFAULT 0,
            cache_has_attachments INTEGER DEFAULT 0
        );
        CREATE TABLE chat_message_join (
            chat_id INTEGER,
            message_id INTEGER
        );
        """
    )
    conn.commit()
    conn.close()


class TestV041RealWorldFixes(unittest.IsolatedAsyncioTestCase):
    def test_version_and_static_safety_audit(self) -> None:
        self.assertEqual(__version__, "0.6.3")
        self.assertIs(InstinctOrchestrator, HyperTextOrchestrator)
        default_cfg = AgentConfig()
        self.assertEqual(default_cfg.heartbeat_interval_sec, 60.0)
        self.assertEqual(default_cfg.max_heartbeat_per_turn, 1)

        cfg_hb_new = AgentConfig.from_env(
            environ={
                "HYPERTEXT_HEARTBEAT_INTERVAL_SEC": "75.5",
                "HEARTBEAT_INTERVAL_SEC": "18.0",
                "HYPERTEXT_MAX_HEARTBEAT_PER_TURN": "2",
                "MAX_HEARTBEAT_PER_TURN": "5",
            }
        )
        self.assertEqual(cfg_hb_new.heartbeat_interval_sec, 75.5)
        self.assertEqual(cfg_hb_new.max_heartbeat_per_turn, 2)

        cfg_hb_fallback = AgentConfig.from_env(
            environ={"HEARTBEAT_INTERVAL_SEC": "45.0", "MAX_HEARTBEAT_PER_TURN": "0"}
        )
        self.assertEqual(cfg_hb_fallback.heartbeat_interval_sec, 45.0)
        self.assertEqual(cfg_hb_fallback.max_heartbeat_per_turn, 0)

        cfg_ht = AgentConfig.from_env(
            environ={"HYPERTEXT_STATE_DIR": "/tmp/ht_primary", "INSTINCT_STATE_DIR": "/tmp/ht_legacy"}
        )
        self.assertEqual(cfg_ht.state_dir, Path("/tmp/ht_primary"))
        cfg_leg = AgentConfig.from_env(environ={"INSTINCT_STATE_DIR": "/tmp/ht_legacy"})
        self.assertEqual(cfg_leg.state_dir, Path("/tmp/ht_legacy"))
        project_root = Path(__file__).resolve().parent.parent
        gitignore_text = (project_root / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".env", gitignore_text)
        self.assertIn("assets/logo-candidates/", gitignore_text)
        self.assertIn("logo-handoff-gpt/", gitignore_text)
        self.assertIn("HyperText-Logo-GPT-Pack.zip", gitignore_text)
        if (project_root / ".git").exists():
            import subprocess

            tracked = subprocess.run(
                ["git", "ls-files", ".env", "deploy/com.plotloop.hypertext-agent.local.plist"],
                cwd=str(project_root),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(tracked.stdout.strip(), "")

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
        forbidden_home = Path.home().as_posix()
        for p in project_root.rglob("*"):
            if (
                any(part in skip_dirs for part in p.parts)
                or not p.is_file()
                or p.suffix in {".pyc", ".pyo", ".zip"}
            ):
                continue
            if (
                p.name in {".env", ".DS_Store"}
                or (p.name.startswith(".env.") and p.name != ".env.example")
                or p.name.endswith((".local.plist", ".log", ".save", ".swp", ".bak", ".zip"))
            ):
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
            self.assertFalse(
                forbidden_home in text,
                f"Hardcoded host home path leaked in {p.relative_to(project_root)}",
            )

    async def test_bugfix1_cross_alias_self_chat_dedup_chat_guid_and_unified_session(self) -> None:
        self.assertIn("send targetMessage to chat id targetHandle", APPLESCRIPT_SEND_IMESSAGE)
        self.assertIn("send theAttachment to chat id targetHandle", APPLESCRIPT_SEND_ATTACHMENT)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            chat_db = tmp_path / "chat.db"
            _init_chat_db_with_guid(chat_db)

            phone = "+8613800138000"
            email = "owner@icloud.com"

            conn = sqlite3.connect(str(chat_db))
            conn.execute("INSERT INTO handle (ROWID, id) VALUES (1, ?), (2, ?)", (phone, email))
            conn.execute(
                "INSERT INTO chat (ROWID, guid, chat_identifier) VALUES (10, ?, ?), (11, ?, ?)",
                (f"iMessage;-;{email}", email, f"iMessage;-;{phone}", phone),
            )
            conn.commit()
            conn.close()

            env_file = tmp_path / "test.env"
            env_file.write_text(
                "\n".join(
                    [
                        "PROVIDER_MODE=deepseek",
                        "DEEPSEEK_API_KEY=sk-valid-v041-key",
                        f"ALLOWED_SENDERS={phone},{email}",
                        "ALLOW_SELF_CHAT=true",
                        "SELF_CHAT_PREFIX=/ai",
                        f"CHAT_DB_PATH={chat_db}",
                        f"STATE_DIR={tmp_path / 'state'}",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            cfg = AgentConfig.from_env(env_file=env_file)
            self.assertEqual(cfg.canonical_admin_sender, phone)
            self.assertEqual(cfg.heartbeat_interval_sec, 60.0)
            self.assertEqual(cfg.max_heartbeat_per_turn, 1)

            store = StateStore(cfg.sqlite_db_path)
            gate = HumanGate(default_timeout=5.0)
            mac_bridge = MacIMessageBridge(config=cfg, store=store, human_gate=gate)
            mac_bridge.align_cursor_to_latest()

            # Insert Row 1 (is_from_me=1, typed to email thread while handle_id points to phone)
            # AND Row 2 (is_from_me=0 mirror arriving via phone caller ID)
            conn = sqlite3.connect(str(chat_db))
            conn.execute(
                "INSERT INTO message (ROWID, guid, text, handle_id, is_from_me) VALUES (1, 'g1', '/ai 记住我喜欢喝热的燕麦拿铁', 1, 1)"
            )
            conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (10, 1)")
            conn.execute(
                "INSERT INTO message (ROWID, guid, text, handle_id, is_from_me) VALUES (2, 'g2', '/ai 记住我喜欢喝热的燕麦拿铁', 1, 0)"
            )
            conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (11, 2)")
            conn.commit()
            conn.close()

            polled = mac_bridge.poll_new_messages()
            # Cross-alias twin deduplicated! Only Row 1 emitted, preferring chat_identifier (email) and pinning chat_guid
            self.assertEqual(len(polled), 1)
            self.assertEqual(polled[0].sender, email)
            self.assertEqual(polled[0].reply_target, f"iMessage;-;{email}")

            # Verify unified session across email & phone in HyperTextOrchestrator + SELF_CHAT_REPLY_TO override
            cfg_pinned = AgentConfig.from_env(env_file=env_file)
            cfg_pinned.self_chat_reply_to = phone
            cfg_pinned.enable_instant_ack = False
            cli_bridge = CliBridge(quiet=True)

            async def memory_llm(kwargs: dict[str, Any], call_num: int) -> _MockCompletion:
                msgs = kwargs["messages"]
                last_msg = msgs[-1]
                if last_msg["role"] == "user" and "记住" in str(last_msg["content"]):
                    return _MockCompletion(
                        choices=[
                            _MockChoice(
                                message=_MockMessage(
                                    content="",
                                    tool_calls=[
                                        _MockToolCall(
                                            id="tc_mem",
                                            function=_MockFunction(
                                                name="save_memory",
                                                arguments=json.dumps(
                                                    {"key": "coffee", "value": "热燕麦拿铁"}
                                                ),
                                            ),
                                        )
                                    ],
                                )
                            )
                        ]
                    )
                if last_msg["role"] == "tool":
                    return _MockCompletion(
                        choices=[_MockChoice(message=_MockMessage(content="记好啦，热燕麦拿铁。"))]
                    )
                # Second turn from phone alias: verify system prompt contains memory saved from email alias!
                sys_text = msgs[0]["content"]
                has_coffee = "热燕麦拿铁" in sys_text
                has_prev_turn = any(
                    m["role"] == "assistant" and "记好啦" in str(m.get("content", ""))
                    for m in msgs
                )
                return _MockCompletion(
                    choices=[
                        _MockChoice(
                            message=_MockMessage(
                                content=f"跨别名记忆同步={has_coffee} 历史同步={has_prev_turn}"
                            )
                        )
                    ]
                )

            orch = HyperTextOrchestrator(
                config=cfg_pinned,
                store=store,
                bridge=cli_bridge,
                human_gate=gate,
                llm_client=_MockAsyncOpenAI(memory_llm),
            )

            # Turn 1 arrives from email alias, reply must be pinned to SELF_CHAT_REPLY_TO (phone)
            await orch.handle_inbound_message(polled[0])
            self.assertEqual(cli_bridge.outbox[-1][0], phone)
            self.assertIn("热燕麦拿铁", cli_bridge.outbox[-1][1])

            # Turn 2 arrives from phone alias -> sees both memory and conversation history from Turn 1!
            turn2 = InboundMessage(
                rowid=3,
                guid="g3",
                sender=phone,
                text="我平时喝什么咖啡？",
                timestamp_unix=time.time(),
                is_from_me=1,
                reply_target=f"iMessage;-;{phone}",
            )
            ans2 = await orch.handle_inbound_message(turn2)
            self.assertEqual(ans2, "跨别名记忆同步=True 历史同步=True")

            # Cross-alias 2FA unlock: pending gate registered on canonical `phone`, unlocked by bare code from `email`!
            wait_task = asyncio.create_task(
                gate.request_input(
                    sender=cfg_pinned.canonical_admin_sender,
                    question="请输入验证码",
                    bridge=cli_bridge,
                    reply_to=phone,
                )
            )
            await asyncio.sleep(0.02)

            conn = sqlite3.connect(str(chat_db))
            conn.execute(
                "INSERT INTO message (ROWID, guid, text, handle_id, is_from_me) VALUES (4, 'g4', '882910', 2, 0)"
            )
            conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (10, 4)")
            conn.commit()
            conn.close()

            polled_2fa = mac_bridge.poll_new_messages()
            self.assertEqual(len(polled_2fa), 1)
            self.assertEqual(polled_2fa[0].text, "882910")
            res_2fa = await orch.handle_inbound_message(polled_2fa[0])
            self.assertIsNone(res_2fa)
            self.assertEqual(await wait_task, "882910")
            store.close()

    async def test_bugfix2_safe_3mode_html_renderer_and_pdf_export_with_fallback(self) -> None:
        # Mode B: HTML fragments passed by LLM (exact reproduction of 北京周末去处_1115.html bug)
        fragment_body = (
            "<h2>先看天气</h2>\n"
            "<p>周六白天多云间晴，最高 16℃，<b>夜间转小雨</b>，体感偏凉。</p>\n"
            '<p class="tip">贴士：去郊区看红叶建议带一件防风外套和折叠伞。</p>\n'
            "<script>alert('xss')</script>\n"
            '<div class="route" onclick="steal()"><h3>推荐路线</h3><ul><li>上午：景山公园俯瞰故宫</li><li>下午：五道营胡同喝手冲咖啡</li></ul></div>'
        )
        rendered_frag = render_mobile_html_card(title="北京周末去处", body=fragment_body)
        self.assertIn("<h2>先看天气</h2>", rendered_frag)
        self.assertIn("<b>夜间转小雨</b>", rendered_frag)
        self.assertIn('<p class="tip">', rendered_frag)
        self.assertIn("<ul><li>上午：景山公园俯瞰故宫</li>", rendered_frag)
        self.assertFalse("&lt;h2&gt;" in rendered_frag)
        self.assertFalse("&lt;p" in rendered_frag)
        self.assertFalse("小纳整理" in rendered_frag)
        self.assertFalse("<script>" in rendered_frag)
        self.assertFalse("onclick=" in rendered_frag)
        # Single <h1> only
        self.assertEqual(rendered_frag.count("<h1"), 1)

        # If body already has <h1>, do not duplicate <h1>
        with_h1 = render_mobile_html_card(title="北京周末去处", body="<h1>自定义主标题</h1><p>正文</p>")
        self.assertEqual(with_h1.count("<h1"), 1)

        # Mode A: Full standalone <!DOCTYPE html> custom webpage passthrough with XSS stripping
        custom_full_html = (
            "<!DOCTYPE html><html lang='zh-CN'><head><title>专属看展指南</title>"
            "<style>body { background: #0f172a; color: #f8fafc; } .gallery-grid { display: grid; gap: 16px; }</style>"
            "</head><body>"
            '<main class="gallery-grid"><section class="card" onload="bad()"><h2>798 艺术区周末特展</h2>'
            '<a href="javascript:evil()">危险链接</a><a href="https://798.example.com">展览官网</a></section></main>'
            "<script>window.hack=1</script><iframe src='https://evil.example.com'></iframe>"
            "</body></html>"
        )
        rendered_full = render_mobile_html_card(title="看展", body=custom_full_html)
        self.assertIn(".gallery-grid { display: grid; gap: 16px; }", rendered_full)
        self.assertIn('<meta charset="utf-8">', rendered_full)
        self.assertIn('name="viewport"', rendered_full)
        self.assertIn("<h2>798 艺术区周末特展</h2>", rendered_full)
        self.assertFalse("<script>" in rendered_full.lower())
        self.assertFalse("<iframe" in rendered_full.lower())
        self.assertFalse("onload=" in rendered_full.lower())
        self.assertFalse("javascript:evil" in rendered_full.lower())

        # Test create_and_send_document(doc_format="pdf") headless Chrome print-to-PDF + HTML fallback
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_chrome = tmp_path / "FakeChrome"
            fake_chrome.write_text("#!/bin/sh\n", encoding="utf-8")
            sender = "+8613800138000"

            cfg = AgentConfig(
                provider_mode="deepseek",
                deepseek_api_key="sk-valid-pdf-key",
                allowed_senders={sender},
                enable_instant_ack=False,
                chromium_executable_path=str(fake_chrome),
                state_dir=tmp_path,
            )
            store = StateStore(cfg.sqlite_db_path)
            bridge = CliBridge(quiet=True)
            gate = HumanGate()
            orch = HyperTextOrchestrator(
                config=cfg,
                store=store,
                bridge=bridge,
                human_gate=gate,
                llm_client=_MockAsyncOpenAI(lambda kw, n: _MockCompletion(choices=[])),
            )

            async def fake_chrome_exec(*cmd: str, **kwargs: Any) -> Any:
                for arg in cmd:
                    if arg.startswith("--print-to-pdf="):
                        pdf_out = Path(arg.split("=", 1)[1])
                        pdf_out.write_bytes(b"%PDF-1.7\nfake pdf content\n%%EOF")
                proc = mock.AsyncMock()
                proc.communicate = mock.AsyncMock(return_value=(b"", b""))
                proc.returncode = 0
                return proc

            with mock.patch("asyncio.create_subprocess_exec", side_effect=fake_chrome_exec):
                res_json = await orch._execute_local_tool(
                    sender=sender,
                    fn_name="create_and_send_document",
                    raw_args=json.dumps(
                        {
                            "title": "北京周末攻略",
                            "content": "<h2>行程安排</h2><p>故宫与景山</p>",
                            "doc_format": "pdf",
                            "filename": "北京周末攻略_1115.pdf",
                        }
                    ),
                )
            parsed_pdf = json.loads(res_json)
            self.assertTrue(parsed_pdf["ok"])
            self.assertEqual(parsed_pdf["doc_format"], "pdf")
            self.assertEqual(parsed_pdf["filename"], "北京周末攻略_1115.pdf")
            self.assertEqual(Path(bridge.attachments[-1][1]).suffix, ".pdf")

            # Fallback to HTML when Chrome is unavailable
            cfg.chromium_executable_path = ""
            with mock.patch.object(orch, "_detect_pdf_chrome_binary", return_value=None):
                res_fb = await orch._execute_local_tool(
                    sender=sender,
                    fn_name="create_and_send_document",
                    raw_args=json.dumps(
                        {
                            "title": "无浏览器回退测试",
                            "content": "<h2>回退卡片</h2><p>依然清晰可读</p>",
                            "doc_format": "pdf",
                            "filename": "回退测试.pdf",
                        }
                    ),
                )
            parsed_fb = json.loads(res_fb)
            self.assertTrue(parsed_fb["ok"])
            self.assertEqual(parsed_fb["doc_format"], "html")
            self.assertEqual(Path(bridge.attachments[-1][1]).suffix, ".html")
            store.close()

    async def test_bugfix3_pure_url_omits_marker_and_standalone_link_bubbles(self) -> None:
        raw_reply = (
            "这周末给你挑了三个适合散步看展的地方，天气凉记得带伞：\n"
            "1. 景山公园万春亭看故宫全景\n"
            "2. 798 艺术区秋季新展\n"
            "3. 国家博物馆特展\n\n"
            "https://jingshan.example.com/guide\n"
            "https://798art.example.com/exhibition\n"
            "https://chnmuseum.example.com/booking\n"
            "https://extra-fourth.example.com/ignored"
        )
        main_txt, urls = split_reply_and_standalone_links(raw_reply, max_links=3)
        self.assertNotIn("https://", main_txt)
        self.assertEqual(
            urls,
            [
                "https://jingshan.example.com/guide",
                "https://798art.example.com/exhibition",
                "https://chnmuseum.example.com/booking",
            ],
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            chat_db = tmp_path / "chat.db"
            _init_chat_db_with_guid(chat_db)
            phone = "+8613800138000"

            conn = sqlite3.connect(str(chat_db))
            conn.execute("INSERT INTO handle (ROWID, id) VALUES (1, ?)", (phone,))
            conn.execute(
                "INSERT INTO chat (ROWID, guid, chat_identifier) VALUES (1, ?, ?)",
                (f"iMessage;-;{phone}", phone),
            )
            conn.commit()
            conn.close()

            cfg = AgentConfig(
                provider_mode="deepseek",
                deepseek_api_key="sk-valid-link-key",
                allowed_senders={phone},
                allow_self_chat=True,
                enable_instant_ack=False,
                split_standalone_links=True,
                chat_db_path=chat_db,
                state_dir=tmp_path / "state",
            )
            store = StateStore(cfg.sqlite_db_path)
            mac_bridge = MacIMessageBridge(config=cfg, store=store)
            mac_bridge.align_cursor_to_latest()

            sent_osascript_payloads: list[tuple[str, str]] = []

            async def fake_osascript(*cmd: str, **kwargs: Any) -> Any:
                # cmd: (osascript, '-e', APPLESCRIPT, target, wire_chunk)
                sent_osascript_payloads.append((cmd[3], cmd[4]))
                proc = mock.AsyncMock()
                proc.communicate = mock.AsyncMock(return_value=(b"", b""))
                proc.returncode = 0
                return proc

            async def link_llm(kwargs: dict[str, Any], call_num: int) -> _MockCompletion:
                return _MockCompletion(
                    choices=[_MockChoice(message=_MockMessage(content=raw_reply))]
                )

            orch = HyperTextOrchestrator(
                config=cfg,
                store=store,
                bridge=mac_bridge,
                human_gate=HumanGate(),
                llm_client=_MockAsyncOpenAI(link_llm),
            )

            with (
                mock.patch("asyncio.create_subprocess_exec", side_effect=fake_osascript),
                mock.patch("asyncio.sleep", return_value=None),
            ):
                await orch.handle_inbound_message(
                    InboundMessage(
                        rowid=1,
                        guid="m1",
                        sender=phone,
                        text="周末去哪玩",
                        timestamp_unix=time.time(),
                        is_from_me=1,
                        reply_target=f"iMessage;-;{phone}",
                    )
                )

            # 1 main text bubble (with BOT_ECHO_MARKER) + 3 pure URL bubbles (NO BOT_ECHO_MARKER!)
            self.assertEqual(len(sent_osascript_payloads), 4)
            self.assertTrue(sent_osascript_payloads[0][1].startswith(BOT_ECHO_MARKER))
            for idx in (1, 2, 3):
                target_sent, wire_url = sent_osascript_payloads[idx]
                self.assertEqual(target_sent, f"iMessage;-;{phone}")
                self.assertFalse(BOT_ECHO_MARKER in wire_url)
                self.assertTrue(wire_url.startswith("https://"))

            # Simulate macOS echoing pure URL back as both is_from_me=1 and is_from_me=0 -> suppressed by Layer 1 SHA-256!
            conn = sqlite3.connect(str(chat_db))
            conn.execute(
                "INSERT INTO message (ROWID, guid, text, handle_id, is_from_me) VALUES (2, 'e1', ?, 1, 1), (3, 'e2', ?, 1, 0)",
                ("https://jingshan.example.com/guide", "https://jingshan.example.com/guide"),
            )
            conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 2), (1, 3)")
            conn.commit()
            conn.close()

            echo_check = mac_bridge.poll_new_messages()
            self.assertEqual(echo_check, [])
            store.close()

    async def test_bugfix4_strict_routing_stage_progress_predoc_notice_and_browser_log_observer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sender = "+8613800138000"
            cfg = AgentConfig(
                provider_mode="deepseek",
                deepseek_api_key="sk-valid-stage-key",
                allowed_senders={sender},
                enable_instant_ack=False,
                heartbeat_interval_sec=0.05,
                state_dir=tmp_path,
            )
            store = StateStore(cfg.sqlite_db_path)
            bridge = CliBridge(quiet=True)
            gate = HumanGate()

            # 1. Check strict Research-vs-Browser routing prompt & tool schema
            orch_prompt_check = HyperTextOrchestrator(
                config=cfg,
                store=store,
                bridge=bridge,
                human_gate=gate,
                llm_client=_MockAsyncOpenAI(lambda kw, n: _MockCompletion(choices=[])),
            )
            tools = orch_prompt_check._build_tools(is_deepseek_native=True)
            browser_tool = next(
                t for t in tools if t.get("type") == "function" and t["function"]["name"] == "run_browser_task"
            )
            self.assertIn("旅游攻略", browser_tool["function"]["description"])
            self.assertIn("严禁", browser_tool["function"]["description"])
            sys_prompt = await orch_prompt_check._build_system_prompt(sender, "北京周末旅游攻略")
            self.assertIn("检索与浏览器严格分流", sys_prompt)
            self.assertIn("严禁调用 `run_browser_task`", sys_prompt)

            # 2. Check pre-document interim notice guard (fires ONLY when ack_sent is False)
            bridge.outbox.clear()
            aged_ctx: dict[str, Any] = {
                "summary": "周末攻略",
                "started_at": time.time() - 6.5,
                "ack_sent": False,
                "pre_doc_notice_sent": False,
                "reply_recipient": sender,
            }
            await orch_prompt_check._execute_local_tool(
                sender=sender,
                fn_name="create_and_send_document",
                raw_args=json.dumps(
                    {"title": "攻略卡片", "content": "<h2>天气</h2><p>晴朗</p>", "doc_format": "html"}
                ),
                turn_ctx=aged_ctx,
            )
            self.assertTrue(
                any("正在给你生成排版好的详细附件" in txt for _, txt in bridge.outbox)
            )

            # When ack_sent is ALREADY True, create_and_send_document sends ZERO extra text bubbles
            bridge.outbox.clear()
            already_acked_ctx: dict[str, Any] = {
                "summary": "周末攻略",
                "started_at": time.time() - 8.0,
                "ack_sent": True,
                "ack_sent_at": time.time() - 6.0,
                "pre_doc_notice_sent": False,
                "reply_recipient": sender,
            }
            await orch_prompt_check._execute_local_tool(
                sender=sender,
                fn_name="create_and_send_document",
                raw_args=json.dumps(
                    {"title": "攻略卡片2", "content": "<h2>路线</h2><p>景山</p>", "doc_format": "html"}
                ),
                turn_ctx=already_acked_ctx,
            )
            self.assertEqual(bridge.outbox, [])

            # 3. Verify BrowserSandbox Single-Exit Quiet-Tool: in-memory log state + 0 outbox messages
            bridge.outbox.clear()
            bu_logger = logging.getLogger("browser_use")
            handlers_before = list(bu_logger.handlers)

            class FakeBrowser:
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    pass

                async def stop(self) -> None:
                    return None

            registered_tool_names: list[str] = []

            class FakeTools:
                def action(self, desc: str) -> Any:
                    def _dec(fn: Any) -> Any:
                        registered_tool_names.append(fn.__name__)
                        return fn

                    return _dec

            captured_extend_prompt: dict[str, str] = {}

            class FakeBrowserAgent:
                def __init__(self, **kwargs: Any) -> None:
                    captured_extend_prompt["msg"] = kwargs.get("extend_system_message", "")

                async def run(self, max_steps: int = 25) -> Any:
                    bu_logger.info("Step 1: Navigating to https://booking.airline-demo.com/flight-status")
                    bu_logger.info("Step 3: Checking seat map on https://booking.airline-demo.com/seats")
                    await asyncio.sleep(0.08)
                    return types.SimpleNamespace(final_result=lambda: "选座核对完成")

            fake_bu = types.ModuleType("browser_use")
            fake_bu.Agent = FakeBrowserAgent  # type: ignore[attr-defined]
            fake_bu.Browser = FakeBrowser  # type: ignore[attr-defined]
            fake_bu.ChatOpenAI = lambda *a, **kw: None  # type: ignore[attr-defined]
            fake_bu.Tools = FakeTools  # type: ignore[attr-defined]

            quiet_turn_ctx: dict[str, Any] = {}
            sandbox = BrowserSandbox(config=cfg, human_gate=gate, bridge=bridge)
            with mock.patch.dict(sys.modules, {"browser_use": fake_bu}):
                res_bu = await sandbox.run_task(
                    sender=sender,
                    task_prompt="核对航班选座",
                    reply_to="iMessage;-;+8613800138000",
                    progress_interval_sec=0.02,
                    turn_ctx=quiet_turn_ctx,
                )

            self.assertEqual(res_bu, "选座核对完成")
            self.assertNotIn("notify_progress_via_imessage", captured_extend_prompt["msg"])
            self.assertNotIn("notify_progress_via_imessage", registered_tool_names)
            self.assertIn("ask_human_via_imessage", registered_tool_names)
            self.assertIn("send_screenshot_or_file_via_imessage", registered_tool_names)
            self.assertIn("1. If the website requests a 2FA/MFA code", captured_extend_prompt["msg"])
            self.assertIn("4. Reply concisely in plain natural language", captured_extend_prompt["msg"])
            self.assertEqual(list(bu_logger.handlers), handlers_before)
            self.assertEqual(bridge.outbox, [])
            b_state = quiet_turn_ctx.get("browser_progress_state")
            self.assertIsInstance(b_state, dict)
            assert isinstance(b_state, dict)
            self.assertEqual(b_state["domain"], "booking.airline-demo.com")
            self.assertEqual(b_state["step"], 3)
            self.assertGreater(float(b_state["updated_at"]), 0.0)
            store.close()

    async def test_v051_single_exit_quiet_tool_short_and_long_browse_turns(self) -> None:
        """Verify <60s browse turn sends 1 ack + 1 answer (0 heartbeats), and >120s 15-step browse turn sends 1 ack + 1 calm reassurance (strictly after ack_sent_at + interval) + 1 answer."""

        class _TimedBridge(CliBridge):
            def __init__(self) -> None:
                super().__init__(quiet=True)
                self.timed_outbox: list[tuple[str, str, float]] = []

            async def send_text(self, recipient: str, text: str) -> None:
                await super().send_text(recipient, text)
                self.timed_outbox.append((recipient, text, time.time()))

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sender = "+8613800138000"
            store = StateStore(tmp_path / "state.sqlite3")
            bridge = _TimedBridge()
            gate = HumanGate()

            bu_logger = logging.getLogger("browser_use")

            class FakeBrowser:
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    pass

                async def stop(self) -> None:
                    return None

            class FakeTools:
                def action(self, desc: str) -> Any:
                    def _dec(fn: Any) -> Any:
                        return fn

                    return _dec

            # --- Case A: < heartbeat_interval_sec (45s equivalent: ack=0.03s, hb=0.25s, run=0.10s) ---
            class ShortBrowserAgent:
                def __init__(self, **kwargs: Any) -> None:
                    pass

                async def run(self, max_steps: int = 25) -> Any:
                    bu_logger.info("Step 1: Opening https://flights.example.com/search")
                    await asyncio.sleep(0.05)
                    bu_logger.info("Step 4: Reading fares on https://flights.example.com/results")
                    await asyncio.sleep(0.05)
                    return types.SimpleNamespace(final_result=lambda: "直飞最低 1120 元")

            fake_bu_short = types.ModuleType("browser_use")
            fake_bu_short.Agent = ShortBrowserAgent  # type: ignore[attr-defined]
            fake_bu_short.Browser = FakeBrowser  # type: ignore[attr-defined]
            fake_bu_short.ChatOpenAI = lambda *a, **kw: None  # type: ignore[attr-defined]
            fake_bu_short.Tools = FakeTools  # type: ignore[attr-defined]

            async def short_llm(kwargs: dict[str, Any], call_num: int) -> _MockCompletion:
                if call_num == 1:
                    return _MockCompletion(
                        choices=[
                            _MockChoice(
                                message=_MockMessage(
                                    content="我先帮你打开网页看看实时票价。",
                                    tool_calls=[
                                        _MockToolCall(
                                            id="tc_short",
                                            function=_MockFunction(
                                                name="run_browser_task",
                                                arguments=json.dumps({"task_prompt": "登录查机票最低价"}),
                                            ),
                                        )
                                    ],
                                )
                            )
                        ]
                    )
                return _MockCompletion(
                    choices=[_MockChoice(message=_MockMessage(content="已核对好，直飞最低 1120 元。"))]
                )

            cfg_short = AgentConfig(
                provider_mode="deepseek",
                deepseek_api_key="sk-valid-v051-short",
                allowed_senders={sender},
                enable_instant_ack=True,
                fast_ack_delay_sec=0.03,
                heartbeat_interval_sec=0.25,
                max_heartbeat_per_turn=1,
                state_dir=tmp_path,
            )
            orch_short = HyperTextOrchestrator(
                config=cfg_short,
                store=store,
                bridge=bridge,
                human_gate=gate,
                llm_client=_MockAsyncOpenAI(short_llm),
            )
            with mock.patch.dict(sys.modules, {"browser_use": fake_bu_short}):
                ans_short = await orch_short.handle_inbound_message(
                    InboundMessage(
                        rowid=1,
                        guid="v051-1",
                        sender=sender,
                        text="帮我打开网页登录查一下机票",
                        timestamp_unix=time.time(),
                    )
                )

            self.assertEqual(ans_short, "已核对好，直飞最低 1120 元。")
            self.assertEqual(
                len(bridge.timed_outbox),
                2,
                f"Expected strictly [ack, final], got: {bridge.timed_outbox}",
            )
            self.assertIn("打开页面去办", bridge.timed_outbox[0][1])
            self.assertEqual(bridge.timed_outbox[1][1], "已核对好，直飞最低 1120 元。")

            # --- Case B: > 2x heartbeat_interval_sec across 15 steps & 3 domains ---
            # ack_delay=0.05s, hb_interval=0.10s, total browser run ~0.30s (> 2.5x hb_interval)
            bridge.outbox.clear()
            bridge.timed_outbox.clear()

            class LongBrowserAgent:
                def __init__(self, **kwargs: Any) -> None:
                    pass

                async def run(self, max_steps: int = 25) -> Any:
                    domains = [
                        "https://alpha-air.example.com/list",
                        "https://beta-ota.example.com/compare",
                        "https://gamma-direct.example.com/checkout",
                    ]
                    for step_idx in range(1, 16):
                        dom = domains[(step_idx - 1) // 5]
                        bu_logger.info(f"Step {step_idx}: Inspecting {dom}?s={step_idx}")
                        await asyncio.sleep(0.02)
                    return types.SimpleNamespace(final_result=lambda: "三家比价完成，官网直订最低 980 元")

            fake_bu_long = types.ModuleType("browser_use")
            fake_bu_long.Agent = LongBrowserAgent  # type: ignore[attr-defined]
            fake_bu_long.Browser = FakeBrowser  # type: ignore[attr-defined]
            fake_bu_long.ChatOpenAI = lambda *a, **kw: None  # type: ignore[attr-defined]
            fake_bu_long.Tools = FakeTools  # type: ignore[attr-defined]

            async def long_llm(kwargs: dict[str, Any], call_num: int) -> _MockCompletion:
                if call_num == 1:
                    return _MockCompletion(
                        choices=[
                            _MockChoice(
                                message=_MockMessage(
                                    content="这段中间过渡思考不该发给用户手机！",
                                    tool_calls=[
                                        _MockToolCall(
                                            id="tc_long",
                                            function=_MockFunction(
                                                name="run_browser_task",
                                                arguments=json.dumps({"task_prompt": "跨三个网站完成15步比价"}),
                                            ),
                                        )
                                    ],
                                )
                            )
                        ]
                    )
                return _MockCompletion(
                    choices=[
                        _MockChoice(
                            message=_MockMessage(content="比价完成，官网直订最低 980 元。")
                        )
                    ]
                )

            cfg_long = AgentConfig(
                provider_mode="deepseek",
                deepseek_api_key="sk-valid-v051-long",
                allowed_senders={sender},
                enable_instant_ack=True,
                fast_ack_delay_sec=0.05,
                heartbeat_interval_sec=0.10,
                max_heartbeat_per_turn=1,
                state_dir=tmp_path,
            )
            orch_long = HyperTextOrchestrator(
                config=cfg_long,
                store=store,
                bridge=bridge,
                human_gate=gate,
                llm_client=_MockAsyncOpenAI(long_llm),
            )

            t_turn_start = time.time()
            with mock.patch.dict(sys.modules, {"browser_use": fake_bu_long}):
                ans_long = await orch_long.handle_inbound_message(
                    InboundMessage(
                        rowid=2,
                        guid="v051-2",
                        sender=sender,
                        text="帮我打开浏览器去三家网站登录后比价",
                        timestamp_unix=t_turn_start,
                    )
                )

            self.assertEqual(ans_long, "比价完成，官网直订最低 980 元。")
            self.assertEqual(
                len(bridge.timed_outbox),
                3,
                f"Expected strictly [ack, 1 calm reassurance, final], got: {bridge.timed_outbox}",
            )
            _, ack_msg, t_ack = bridge.timed_outbox[0]
            _, hb_msg, t_hb = bridge.timed_outbox[1]
            _, final_msg, t_final = bridge.timed_outbox[2]

            self.assertIn("打开页面去办", ack_msg)
            self.assertEqual(hb_msg, "内容稍微有点多，还在做最后的核对整理，马上好。")
            self.assertEqual(final_msg, "比价完成，官网直订最低 980 元。")

            # Heartbeat MUST fire strictly after ack_sent_at + heartbeat_interval_sec (NOT started_at + hb_interval)
            self.assertGreaterEqual(t_hb - t_ack, 0.095)
            self.assertGreater(t_final, t_hb)

            # Zero plumbing, zero domains, zero step numbers, zero intermediate assistant.content leaked
            all_sent_texts = "\n".join(txt for _, txt, _ in bridge.timed_outbox)
            for forbidden_leak in (
                "这段中间过渡思考不该发给用户手机",
                "alpha-air.example.com",
                "beta-ota.example.com",
                "gamma-direct.example.com",
                "第 15 步",
                "Step ",
                "浏览器交互操作",
            ):
                self.assertNotIn(forbidden_leak, all_sent_texts)

            store.close()


if __name__ == "__main__":
    unittest.main()
