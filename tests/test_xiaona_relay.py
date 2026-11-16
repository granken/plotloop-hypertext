"""Comprehensive unit tests for HyperText v0.5.2 Explicit-Only Admin XiaoNa Relay integration."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import sqlite3
import tempfile
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

from hypertext import (
    BOT_ECHO_MARKER,
    AgentConfig,
    CliBridge,
    HumanGate,
    HyperTextOrchestrator,
    InboundMessage,
    MacIMessageBridge,
    StateStore,
    XiaoNaClient,
    extract_xiaona_query,
    format_xiaona_reply,
    is_compound_xiaona_request,
    is_explicit_xiaona_request,
    is_xiaomiguan_alias_used,
    is_xiaona_reset_command,
    normalize_xiaona_ask_url,
    normalize_xiaona_health_url,
    run_doctor,
)


@dataclass
class _MockFunction:
    name: str
    arguments: str


@dataclass
class _MockToolCall:
    id: str
    function: _MockFunction
    type: str = "function"


@dataclass
class _MockMessage:
    content: str | None = ""
    reasoning_content: str | None = None
    tool_calls: list[_MockToolCall] | None = None
    server_tool_calls: list[dict[str, Any]] | None = None


@dataclass
class _MockChoice:
    message: _MockMessage


@dataclass
class _MockCompletion:
    choices: list[_MockChoice]


class _MockCompletionsEndpoint:
    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _MockCompletion:
        self.calls.append(kwargs)
        return await self._handler(kwargs, len(self.calls))


class _MockChatNamespace:
    def __init__(self, handler: Any) -> None:
        self.completions = _MockCompletionsEndpoint(handler)


class _MockAsyncOpenAI:
    def __init__(self, handler: Any) -> None:
        self.chat = _MockChatNamespace(handler)


def _init_self_chat_db(db_path: Path, admin_phone: str) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        f"""
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL);
        CREATE TABLE chat (
            ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
            guid TEXT NOT NULL,
            chat_identifier TEXT NOT NULL
        );
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
            guid TEXT,
            handle_id INTEGER,
            text TEXT,
            attributedBody BLOB,
            cache_has_attachments INTEGER DEFAULT 0,
            date INTEGER DEFAULT 0,
            is_from_me INTEGER DEFAULT 0,
            associated_message_type INTEGER DEFAULT 0
        );
        INSERT INTO handle(ROWID, id) VALUES (1, '{admin_phone}');
        INSERT INTO chat(ROWID, guid, chat_identifier)
            VALUES (1, 'iMessage;-;{admin_phone}', '{admin_phone}');
        """
    )
    conn.commit()
    conn.close()


class TestXiaoNaRelayIntegration(unittest.IsolatedAsyncioTestCase):
    def test_url_helpers_and_trigger_parsers(self) -> None:
        self.assertEqual(normalize_xiaona_ask_url(""), "")
        self.assertEqual(
            normalize_xiaona_ask_url("https://xiaona-relay.example.com/"),
            "https://xiaona-relay.example.com/ask",
        )
        self.assertEqual(
            normalize_xiaona_ask_url("https://xiaona-relay.example.com/health"),
            "https://xiaona-relay.example.com/ask",
        )
        self.assertEqual(
            normalize_xiaona_ask_url("https://xiaona-relay.example.com/ask/"),
            "https://xiaona-relay.example.com/ask",
        )
        self.assertEqual(
            normalize_xiaona_health_url("https://xiaona-relay.example.com/ask"),
            "https://xiaona-relay.example.com/health",
        )

        # Positive explicit triggers (including Chinese immediately after `xiaona`)
        for positive in (
            "/xiaona",
            "/小纳",
            "/xiaona 今天有什么日程",
            "/XiaoNa查一下周报",
            "/小纳 今天有什么日程",
            "@小纳 帮我看下",
            "@xiaona check schedule",
            "小纳：今天有什么安排",
            "小纳 今天有什么安排",
            "问下小纳今天有什么会",
            "问一下xiaona昨天的纪要",
            "让小纳帮我看下昨天的录音总结",
            "跟小纳确认下周会时间",
            "小纳那边有什么结论",
            "/xiaona reset",
            "重置小纳会话",
        ):
            self.assertTrue(
                is_explicit_xiaona_request(positive),
                f"Expected explicit trigger for: {positive!r}",
            )

        # Negative triggers (ordinary queries & word-boundary false friends)
        for negative in (
            "",
            "今天有什么日程",
            "帮我查一下上海天气",
            "/xiaonabot 你好",
            "myxiaona test",
            "小纳米音箱怎么连蓝牙",
            "买牛奶",
        ):
            self.assertFalse(
                is_explicit_xiaona_request(negative),
                f"Expected NON-trigger for: {negative!r}",
            )

        # Reset commands
        for reset_txt in (
            "/xiaona reset",
            "/xiaona clear",
            "/小纳 reset",
            "/小纳 重置",
            "重置小纳会话",
            "清空小纳会话",
            "重置小纳上下文",
        ):
            self.assertTrue(is_xiaona_reset_command(reset_txt))
        self.assertFalse(is_xiaona_reset_command("/xiaona 今天有什么日程"))

        # Query extraction
        self.assertEqual(extract_xiaona_query("/xiaona"), "")
        self.assertEqual(extract_xiaona_query("/小纳"), "")
        self.assertEqual(extract_xiaona_query("/xiaona 今天有什么日程"), "今天有什么日程")
        self.assertEqual(
            extract_xiaona_query("让小纳帮我看下昨天的录音总结"),
            "帮我看下昨天的录音总结",
        )
        self.assertEqual(
            extract_xiaona_query("请小纳回答：明天的重点议程"),
            "明天的重点议程",
        )

        # Compound detection
        self.assertFalse(is_compound_xiaona_request("/xiaona 今天有什么日程"))
        self.assertTrue(
            is_compound_xiaona_request("让小纳查一下明天会议，并且帮我设个提醒")
        )
        self.assertTrue(
            is_compound_xiaona_request("/xiaona 查完后用本地浏览器打开并截屏")
        )

    async def test_1_non_explicit_queries_never_expose_or_call_xiaona(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            admin = "+8613800138000"
            cfg = AgentConfig(
                provider_mode="deepseek",
                deepseek_api_key="sk-valid-ds-key",
                allowed_senders={admin},
                enable_instant_ack=False,
                xiaona_relay_url="https://xiaona-relay.example.com/ask",
                xiaona_relay_key="xn_test_valid_key",
                state_dir=tmp_path,
            )
            store = StateStore(cfg.sqlite_db_path)
            bridge = CliBridge(quiet=True)
            gate = HumanGate()

            xn_calls = 0

            async def fail_if_called(
                url: str, body: bytes, headers: dict[str, str], timeout_sec: float
            ) -> tuple[int, bytes]:
                nonlocal xn_calls
                xn_calls += 1
                raise AssertionError("XiaoNaClient must NEVER be called for ordinary messages!")

            xn_client = XiaoNaClient(
                cfg,
                transport=fail_if_called,
                enable_keychain_fallback=False,
                allow_repo_dotenv_fallback=False,
            )

            seen_tools: list[list[dict[str, Any]]] = []

            async def ordinary_llm(kwargs: dict[str, Any], call_num: int) -> _MockCompletion:
                seen_tools.append(kwargs.get("tools", []))
                return _MockCompletion(
                    choices=[_MockChoice(message=_MockMessage(content="上海今天多云 23 度。"))]
                )

            orch = HyperTextOrchestrator(
                config=cfg,
                store=store,
                bridge=bridge,
                human_gate=gate,
                llm_client=_MockAsyncOpenAI(ordinary_llm),
                xiaona_client=xn_client,
            )

            # Verify default _build_tools excludes ask_xiaona in both DeepSeek and OpenAI modes
            for ds_flag in (True, False):
                fn_names = [
                    t["function"]["name"]
                    for t in orch._build_tools(is_deepseek_native=ds_flag)
                    if t.get("type") == "function"
                ]
                self.assertNotIn("ask_xiaona", fn_names)

            ans = await orch.handle_inbound_message(
                InboundMessage(
                    rowid=1,
                    guid="ord-1",
                    sender=admin,
                    text="帮我查一下上海今天天气",
                    timestamp_unix=time.time(),
                )
            )
            self.assertEqual(ans, "上海今天多云 23 度。")
            self.assertEqual(xn_calls, 0)
            self.assertEqual(len(seen_tools), 1)
            exposed_fns = [
                t["function"]["name"]
                for t in seen_tools[0]
                if t.get("type") == "function"
            ]
            self.assertNotIn("ask_xiaona", exposed_fns)
            store.close()

    async def test_2_explicit_admin_queries_ack_media_urls_history_and_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            admin = "+8613800138000"
            cfg = AgentConfig(
                provider_mode="deepseek",
                deepseek_api_key="sk-valid-ds-key",
                allowed_senders={admin},
                enable_instant_ack=True,
                fast_ack_delay_sec=0.04,
                heartbeat_interval_sec=60.0,
                max_heartbeat_per_turn=1,
                split_standalone_links=True,
                xiaona_relay_url="https://xiaona-relay.example.com",
                xiaona_relay_key="xn_admin_secret_token",
                state_dir=tmp_path,
            )
            store = StateStore(cfg.sqlite_db_path)
            bridge = CliBridge(quiet=True)
            gate = HumanGate()

            captured_requests: list[dict[str, Any]] = []

            async def mock_xn_transport(
                url: str, body: bytes, headers: dict[str, str], timeout_sec: float
            ) -> tuple[int, bytes]:
                await asyncio.sleep(0.10)  # Exceeds 0.04s fast_ack_delay_sec -> triggers single ack
                payload = json.loads(body.decode("utf-8"))
                captured_requests.append(
                    {
                        "url": url,
                        "payload": payload,
                        "headers": dict(headers),
                        "timeout": timeout_sec,
                    }
                )
                resp = {
                    "reply": "### 今日安排\n- **14:00** 产品周会\n- **16:30** 架构评审",
                    "media_urls": [
                        "https://xiaona-relay.example.com/cards/schedule.png",
                        "https://xiaona-relay.example.com/docs/brief.pdf",
                    ],
                    "session_id": "ext-9901",
                    "duration_ms": 1820,
                }
                return (200, json.dumps(resp, ensure_ascii=False).encode("utf-8"))

            async def fallback_only_dl(url: str) -> Path | None:
                return None

            xn_client = XiaoNaClient(
                cfg,
                transport=mock_xn_transport,
                media_downloader=fallback_only_dl,
                enable_keychain_fallback=False,
                allow_repo_dotenv_fallback=False,
            )

            async def llm_must_not_run(kwargs: dict[str, Any], call_num: int) -> _MockCompletion:
                raise AssertionError("Direct XiaoNa turn must bypass DeepSeek LLM!")

            orch = HyperTextOrchestrator(
                config=cfg,
                store=store,
                bridge=bridge,
                human_gate=gate,
                llm_client=_MockAsyncOpenAI(llm_must_not_run),
                xiaona_client=xn_client,
            )

            # Bare `/xiaona` prompts user for content without firing HTTP
            bare_ans = await orch.handle_inbound_message(
                InboundMessage(
                    rowid=1,
                    guid="xn-bare",
                    sender=admin,
                    text="/xiaona",
                    timestamp_unix=time.time(),
                )
            )
            self.assertEqual(bare_ans, "请直接附上要问小纳的内容，例如：/xiaona 今天有什么日程")
            self.assertEqual(len(captured_requests), 0)

            # Explicit `/xiaona ...` turn
            bridge.outbox.clear()
            ans = await orch.handle_inbound_message(
                InboundMessage(
                    rowid=2,
                    guid="xn-ask-1",
                    sender=admin,
                    text="/xiaona 今天有什么日程",
                    timestamp_unix=time.time(),
                )
            )
            self.assertIsNotNone(ans)
            self.assertEqual(len(captured_requests), 1)
            self.assertEqual(captured_requests[0]["url"], "https://xiaona-relay.example.com/ask")
            self.assertEqual(captured_requests[0]["payload"]["message"], "今天有什么日程")
            self.assertEqual(captured_requests[0]["payload"]["conversation_id"], "imessage-owner")
            self.assertEqual(
                captured_requests[0]["headers"]["Authorization"],
                "Bearer xn_admin_secret_token",
            )
            self.assertGreaterEqual(captured_requests[0]["timeout"], 600.0)

            # Outbox: [0] ack, [1] formatted SMS body (zero Markdown), [2..3] standalone media_urls
            self.assertEqual(len(bridge.outbox), 4, f"Unexpected outbox: {bridge.outbox}")
            self.assertEqual(bridge.outbox[0][1], "收到，我问一下小纳，好了回你。")
            self.assertIn("14:00 产品周会", bridge.outbox[1][1])
            self.assertNotIn("**", bridge.outbox[1][1])
            self.assertNotIn("###", bridge.outbox[1][1])
            self.assertEqual(
                bridge.outbox[2][1],
                "https://xiaona-relay.example.com/cards/schedule.png",
            )
            self.assertEqual(
                bridge.outbox[3][1],
                "https://xiaona-relay.example.com/docs/brief.pdf",
            )

            # Verify Q&A persisted in SQLite conversation history
            hist = store.get_conversation_history(admin, limit=10, is_deepseek_native=True)
            self.assertEqual(len(hist), 2)
            self.assertEqual(hist[0]["role"], "user")
            self.assertEqual(hist[0]["content"], "/xiaona 今天有什么日程")
            self.assertEqual(hist[1]["role"], "assistant")
            self.assertIn("14:00 产品周会", hist[1]["content"])

            # `/xiaona reset` rotates conversation_id (`imessage-owner-<ts>`)
            reset_ans = await orch.handle_inbound_message(
                InboundMessage(
                    rowid=3,
                    guid="xn-reset",
                    sender=admin,
                    text="/xiaona reset",
                    timestamp_unix=time.time(),
                )
            )
            self.assertEqual(reset_ans, "🔄 已重置小纳会话上下文，下一条消息将开启全新对话。")

            await orch.handle_inbound_message(
                InboundMessage(
                    rowid=4,
                    guid="xn-ask-2",
                    sender=admin,
                    text="问下小纳明天上午有什么安排",
                    timestamp_unix=time.time(),
                )
            )
            self.assertEqual(len(captured_requests), 2)
            rotated_cid = captured_requests[1]["payload"]["conversation_id"]
            self.assertTrue(rotated_cid.startswith("imessage-owner-"))
            self.assertNotEqual(rotated_cid, "imessage-owner")
            self.assertLessEqual(len(rotated_cid), 64)
            self.assertEqual(captured_requests[1]["payload"]["message"], "明天上午有什么安排")
            store.close()

    async def test_3_per_conversation_lock_payload_cap_and_zero_leak_errors(self) -> None:
        secret_key = "xn_super_secret_token_998877"
        private_url = "https://xiaona-relay.example.com/ask"
        cfg = AgentConfig(
            xiaona_relay_url=private_url,
            xiaona_relay_key=secret_key,
            xiaona_timeout_sec=300.0,
        )

        in_flight = 0
        max_in_flight = 0
        call_order: list[str] = []

        async def serial_check_transport(
            url: str, body: bytes, headers: dict[str, str], timeout_sec: float
        ) -> tuple[int, bytes]:
            nonlocal in_flight, max_in_flight
            payload = json.loads(body.decode("utf-8"))
            msg = payload["message"]
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            call_order.append(f"start:{msg}")
            await asyncio.sleep(0.06)
            call_order.append(f"end:{msg}")
            in_flight -= 1
            return (
                200,
                json.dumps({"reply": f"done:{msg}", "media_urls": []}).encode("utf-8"),
            )

        client = XiaoNaClient(
            cfg,
            transport=serial_check_transport,
            enable_keychain_fallback=False,
            allow_repo_dotenv_fallback=False,
        )
        r1, r2 = await asyncio.gather(
            client.ask("first", conversation_id="imessage-owner"),
            client.ask("second", conversation_id="imessage-owner"),
        )
        self.assertTrue(r1.ok and r2.ok)
        self.assertEqual(max_in_flight, 1)
        self.assertEqual(
            call_order,
            ["start:first", "end:first", "start:second", "end:second"],
        )

        # < 60KB payload cap guard (zero HTTP calls)
        transport_invoked = False

        async def unused_transport(
            url: str, body: bytes, headers: dict[str, str], timeout_sec: float
        ) -> tuple[int, bytes]:
            nonlocal transport_invoked
            transport_invoked = True
            return (200, b"{}")

        guard_client = XiaoNaClient(
            cfg,
            transport=unused_transport,
            enable_keychain_fallback=False,
            allow_repo_dotenv_fallback=False,
        )
        oversized = "超长文本" * 6000  # > 60KB UTF-8
        res_413 = await guard_client.ask(oversized)
        self.assertFalse(res_413.ok)
        self.assertEqual(res_413.status_code, 413)
        self.assertIn("60KB", res_413.formatted_reply)
        self.assertFalse(transport_invoked)

        # 400 / 401 / 502 (with leaked token & URL inside error) / timeout / network errors
        for code, raw_body, expected_substr in (
            (400, b'{"error":"missing field"}', "400"),
            (401, f'{{"error":"invalid token {secret_key}"}}'.encode("utf-8"), "XIAONA_RELAY_KEY"),
            (
                502,
                json.dumps(
                    {
                        "error": f"upstream failed for {private_url} with Bearer {secret_key}"
                    }
                ).encode("utf-8"),
                "502",
            ),
        ):
            async def status_transport(
                url: str,
                body: bytes,
                headers: dict[str, str],
                timeout_sec: float,
                _c: int = code,
                _b: bytes = raw_body,
            ) -> tuple[int, bytes]:
                return (_c, _b)

            err_client = XiaoNaClient(
                cfg,
                transport=status_transport,
                enable_keychain_fallback=False,
                allow_repo_dotenv_fallback=False,
            )
            err_res = await err_client.ask("测试错误脱敏")
            self.assertFalse(err_res.ok)
            self.assertIn(expected_substr, err_res.formatted_reply)
            self.assertNotIn(secret_key, err_res.formatted_reply)
            self.assertNotIn(private_url, err_res.formatted_reply)

        async def slow_timeout_transport(
            url: str, body: bytes, headers: dict[str, str], timeout_sec: float
        ) -> tuple[int, bytes]:
            await asyncio.sleep(0.5)
            return (200, b'{"reply":"late"}')

        timeout_client = XiaoNaClient(
            cfg,
            transport=slow_timeout_transport,
            enable_keychain_fallback=False,
            allow_repo_dotenv_fallback=False,
            timeout_override=0.03,
        )
        res_to = await timeout_client.ask("测试超时")
        self.assertFalse(res_to.ok)
        self.assertIn("超时", res_to.formatted_reply)
        self.assertNotIn(secret_key, res_to.formatted_reply)
        self.assertNotIn(private_url, res_to.formatted_reply)

    async def test_4_non_admin_beta_member_strictly_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            admin = "+8613800138000"
            member = "+8613900001111"
            cfg = AgentConfig(
                provider_mode="deepseek",
                deepseek_api_key="sk-valid-ds-key",
                allowed_senders={admin},
                enable_invite_codes=True,
                invite_codes=["HT-BETA01"],
                xiaona_relay_url="https://xiaona-relay.example.com/ask",
                xiaona_relay_key="xn_owner_only_key",
                state_dir=tmp_path,
            )
            store = StateStore(cfg.sqlite_db_path)
            bridge = CliBridge(quiet=True)
            gate = HumanGate()

            http_hits = 0

            async def count_transport(
                url: str, body: bytes, headers: dict[str, str], timeout_sec: float
            ) -> tuple[int, bytes]:
                nonlocal http_hits
                http_hits += 1
                return (200, b'{"reply":"leaked!"}')

            xn_client = XiaoNaClient(
                cfg,
                transport=count_transport,
                enable_keychain_fallback=False,
                allow_repo_dotenv_fallback=False,
            )
            orch = HyperTextOrchestrator(
                config=cfg,
                store=store,
                bridge=bridge,
                human_gate=gate,
                xiaona_client=xn_client,
            )

            # Activate member via invite code
            welcome = await orch.handle_inbound_message(
                InboundMessage(
                    rowid=1,
                    guid="m-inv",
                    sender=member,
                    text="HT-BETA01",
                    timestamp_unix=time.time(),
                )
            )
            self.assertIn("激活成功", welcome or "")

            expected_deny = "小纳连接的是主理人私人工作台，仅限主理人本人调用；其他问题我可以直接帮你处理。"
            for idx, attempt in enumerate(
                ("/xiaona 今天有什么日程", "让小纳帮我查一下合同", "/小纳 reset"),
                start=2,
            ):
                denied = await orch.handle_inbound_message(
                    InboundMessage(
                        rowid=idx,
                        guid=f"m-deny-{idx}",
                        sender=member,
                        text=attempt,
                        timestamp_unix=time.time(),
                    )
                )
                self.assertEqual(denied, expected_deny)

            # Also verify direct tool gate `_execute_local_tool("ask_xiaona")`
            raw_tool_res = await orch._execute_local_tool(
                member,
                "ask_xiaona",
                json.dumps({"message": "绕过尝试"}),
            )
            parsed_tool_res = json.loads(raw_tool_res)
            self.assertFalse(parsed_tool_res["ok"])
            self.assertIn("仅限主理人本人调用", parsed_tool_res["error"])
            self.assertEqual(http_hits, 0)
            store.close()

    async def test_5_hot_reload_from_dotenv_keychain_and_doctor_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            admin = "+8613800138000"
            env_file = tmp_path / ".env"
            env_file.write_text(
                f"ALLOWED_SENDERS={admin}\n"
                "DEEPSEEK_API_KEY=sk-valid-hot-key\n"
                "XIAONA_RELAY_URL=https://xiaona-relay.example.com/ask\n",
                encoding="utf-8",
            )
            cfg = AgentConfig.from_env(
                env_file=env_file,
                environ={
                    "ALLOWED_SENDERS": admin,
                    "DEEPSEEK_API_KEY": "sk-valid-hot-key",
                    "ENABLE_INSTANT_ACK": "false",
                    "HYPERTEXT_STATE_DIR": str(tmp_path / "state"),
                    "XIAONA_RELAY_URL": "https://xiaona-relay.example.com/ask",
                    "XIAONA_RELAY_KEY": "",
                },
            )
            store = StateStore(cfg.sqlite_db_path)
            bridge = CliBridge(quiet=True)
            gate = HumanGate()

            seen_auth_headers: list[str] = []

            async def hot_transport(
                url: str, body: bytes, headers: dict[str, str], timeout_sec: float
            ) -> tuple[int, bytes]:
                seen_auth_headers.append(headers.get("Authorization", ""))
                return (
                    200,
                    json.dumps({"reply": "热加载成功：明日无早会。", "media_urls": []}, ensure_ascii=False).encode(
                        "utf-8"
                    ),
                )

            xn_client = XiaoNaClient(
                cfg,
                transport=hot_transport,
                enable_keychain_fallback=False,
                allow_repo_dotenv_fallback=False,
            )
            orch = HyperTextOrchestrator(
                config=cfg,
                store=store,
                bridge=bridge,
                human_gate=gate,
                xiaona_client=xn_client,
            )

            # Call 1: key missing -> returns 1-line setup hint with exact env_file path
            first_reply = await orch.handle_inbound_message(
                InboundMessage(
                    rowid=1,
                    guid="hot-1",
                    sender=admin,
                    text="/xiaona 明天有早会吗",
                    timestamp_unix=time.time(),
                )
            )
            self.assertEqual(
                first_reply,
                f"小纳通道已接入，还差一步配置密钥：把 XIAONA_RELAY_KEY=xn_... 写入 {env_file}"
                "（或存入钥匙串 XIAONA_RELAY_KEY）即可免重启直接调用。",
            )
            self.assertEqual(len(seen_auth_headers), 0)

            # Write XIAONA_RELAY_KEY to .env WITHOUT recreating HyperTextOrchestrator!
            env_file.write_text(
                f"ALLOWED_SENDERS={admin}\n"
                "DEEPSEEK_API_KEY=sk-valid-hot-key\n"
                "XIAONA_RELAY_URL=https://xiaona-relay.example.com/ask\n"
                "XIAONA_RELAY_KEY=xn_test_hot_key\n",
                encoding="utf-8",
            )
            second_reply = await orch.handle_inbound_message(
                InboundMessage(
                    rowid=2,
                    guid="hot-2",
                    sender=admin,
                    text="/xiaona 明天有早会吗",
                    timestamp_unix=time.time(),
                )
            )
            self.assertEqual(second_reply, "热加载成功：明日无早会。")
            self.assertEqual(seen_auth_headers, ["Bearer xn_test_hot_key"])

            # Verify `--doctor` prints masked boolean status with ZERO URL/key plaintext
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = run_doctor(cfg)
            self.assertEqual(rc, 0)
            doctor_out = buf.getvalue()
            self.assertIn(
                "[INFO] XiaoNa Relay (显式唤醒通道): URL configured=True | Key configured=True (hot-reload ready)",
                doctor_out,
            )
            self.assertNotIn("xn_test_hot_key", doctor_out)
            self.assertNotIn("xiaona-relay.example.com", doctor_out)

            # Also verify macOS Keychain fallback when .env has no key
            kc_cfg = AgentConfig(
                xiaona_relay_url="https://xiaona-relay.example.com/ask",
                xiaona_relay_key="",
                state_dir=tmp_path / "kc_state",
            )
            kc_client = XiaoNaClient(
                kc_cfg,
                enable_keychain_fallback=True,
                allow_repo_dotenv_fallback=False,
            )
            fake_proc = mock.Mock(returncode=0, stdout="xn_from_keychain_ok\n")
            with (
                mock.patch("hypertext.xiaona_client.sys.platform", "darwin"),
                mock.patch("hypertext.xiaona_client.subprocess.run", return_value=fake_proc),
            ):
                u_kc, k_kc, src_kc = kc_client.resolve_credentials()
            self.assertEqual(u_kc, "https://xiaona-relay.example.com/ask")
            self.assertEqual(k_kc, "xn_from_keychain_ok")
            self.assertEqual(src_kc, "keychain")
            store.close()

    async def test_6_self_chat_accepts_slash_xiaona_while_preserving_4layer_antiloop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            admin_phone = "+8613800138000"
            chat_db = tmp_path / "chat.db"
            _init_self_chat_db(chat_db, admin_phone)

            cfg = AgentConfig(
                allowed_senders={admin_phone},
                allow_self_chat=True,
                self_chat_prefix="/ai",
                chat_db_path=chat_db,
                state_dir=tmp_path / "state",
            )
            store = StateStore(cfg.sqlite_db_path)
            gate = HumanGate()
            bridge = MacIMessageBridge(config=cfg, store=store, human_gate=gate)
            bridge.align_cursor_to_latest()

            # Simulate outbound assistant echo cached in Layer 1
            bridge.record_outbound_echo("收到，我问一下小纳，好了回你。")

            conn = sqlite3.connect(str(chat_db))
            rows_to_insert = [
                # 1. Plain personal note -> ignored on both rows
                (1, "sc-1", 1, "买牛奶", 1),
                (2, "sc-2", 1, "买牛奶", 0),
                # 2. Word-boundary false prefix `/xiaonabot` -> rejected!
                (3, "sc-3", 1, "/xiaonabot 测试", 1),
                (4, "sc-4", 1, "/xiaonabot 测试", 0),
                # 3. Twin rows for `/xiaona 今天有什么日程` -> emitted ONCE with `/xiaona` intact!
                (5, "sc-5", 1, "/xiaona 今天有什么日程", 1),
                (6, "sc-6", 1, "/xiaona 今天有什么日程", 0),
                # 4. Twin rows for `/小纳 查一下周报` -> emitted ONCE with `/小纳` intact!
                (7, "sc-7", 1, "/小纳 查一下周报", 1),
                (8, "sc-8", 1, "/小纳 查一下周报", 0),
                # 5. Twin rows for `/ai 查天气` -> emitted ONCE with `/ai` stripped!
                (9, "sc-9", 1, "/ai 查天气", 1),
                (10, "sc-10", 1, "/ai 查天气", 0),
                # 6. Assistant outbound echoes (with & without invisible marker) -> suppressed!
                (11, "sc-11", 1, f"{BOT_ECHO_MARKER}/xiaona 这是一条机器人回显", 1),
                (12, "sc-12", 1, "收到，我问一下小纳，好了回你。", 0),
            ]
            for rid, guid, hid, txt, from_me in rows_to_insert:
                conn.execute(
                    "INSERT INTO message(ROWID, guid, handle_id, text, is_from_me, associated_message_type) "
                    "VALUES (?, ?, ?, ?, ?, 0)",
                    (rid, guid, hid, txt, from_me),
                )
                conn.execute("INSERT INTO chat_message_join(chat_id, message_id) VALUES (1, ?)", (rid,))
            conn.commit()
            conn.close()

            emitted = bridge.poll_new_messages()
            self.assertEqual(
                [m.text for m in emitted],
                ["/xiaona 今天有什么日程", "/小纳 查一下周报", "查天气"],
            )
            self.assertTrue(is_explicit_xiaona_request(emitted[0].text))
            self.assertTrue(is_explicit_xiaona_request(emitted[1].text))
            self.assertFalse(is_explicit_xiaona_request(emitted[2].text))
            store.close()

    async def test_7_compound_explicit_request_combines_ask_xiaona_and_schedule_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            admin = "+8613800138000"
            cfg = AgentConfig(
                provider_mode="deepseek",
                deepseek_api_key="sk-valid-compound-key",
                allowed_senders={admin},
                enable_instant_ack=False,
                xiaona_relay_url="https://xiaona-relay.example.com/ask",
                xiaona_relay_key="xn_compound_key",
                state_dir=tmp_path,
            )
            store = StateStore(cfg.sqlite_db_path)
            bridge = CliBridge(quiet=True)
            gate = HumanGate()

            xn_queries: list[str] = []

            async def compound_xn_transport(
                url: str, body: bytes, headers: dict[str, str], timeout_sec: float
            ) -> tuple[int, bytes]:
                payload = json.loads(body.decode("utf-8"))
                xn_queries.append(payload["message"])
                return (
                    200,
                    json.dumps(
                        {"reply": "明天上午10点有战略周会。", "media_urls": []},
                        ensure_ascii=False,
                    ).encode("utf-8"),
                )

            xn_client = XiaoNaClient(
                cfg,
                transport=compound_xn_transport,
                enable_keychain_fallback=False,
                allow_repo_dotenv_fallback=False,
            )

            seen_tool_names: list[list[str]] = []

            async def compound_llm(kwargs: dict[str, Any], call_num: int) -> _MockCompletion:
                fn_names = [
                    t["function"]["name"]
                    for t in kwargs.get("tools", [])
                    if t.get("type") == "function"
                ]
                seen_tool_names.append(fn_names)
                if call_num == 1:
                    return _MockCompletion(
                        choices=[
                            _MockChoice(
                                message=_MockMessage(
                                    content="",
                                    reasoning_content="先问小纳明天会议时间，再设提醒。",
                                    tool_calls=[
                                        _MockToolCall(
                                            id="tc_xn_1",
                                            function=_MockFunction(
                                                name="ask_xiaona",
                                                arguments=json.dumps(
                                                    {"message": "查一下明天会议安排"},
                                                    ensure_ascii=False,
                                                ),
                                            ),
                                        ),
                                        _MockToolCall(
                                            id="tc_sched_1",
                                            function=_MockFunction(
                                                name="schedule_task",
                                                arguments=json.dumps(
                                                    {
                                                        "task_type": "direct_notify",
                                                        "payload": "明天上午10点战略周会准备",
                                                        "delay_seconds": 3600,
                                                    },
                                                    ensure_ascii=False,
                                                ),
                                            ),
                                        ),
                                    ],
                                )
                            )
                        ]
                    )
                return _MockCompletion(
                    choices=[
                        _MockChoice(
                            message=_MockMessage(
                                content="问过小纳了：明天上午10点有战略周会，已帮你设好1小时后的提醒。",
                                reasoning_content="",
                            )
                        )
                    ]
                )

            orch = HyperTextOrchestrator(
                config=cfg,
                store=store,
                bridge=bridge,
                human_gate=gate,
                llm_client=_MockAsyncOpenAI(compound_llm),
                xiaona_client=xn_client,
            )

            ans = await orch.handle_inbound_message(
                InboundMessage(
                    rowid=1,
                    guid="cmp-1",
                    sender=admin,
                    text="让小纳查一下明天会议，并且帮我设个提醒",
                    timestamp_unix=time.time(),
                )
            )
            self.assertIn("明天上午10点有战略周会", ans or "")
            self.assertIn("ask_xiaona", seen_tool_names[0])
            self.assertEqual(xn_queries, ["查一下明天会议安排"])
            tasks = store.list_scheduled_tasks(sender=admin, only_enabled=True)
            self.assertEqual(len(tasks), 1)
            self.assertIn("战略周会", tasks[0]["payload"])
            store.close()

    async def test_8_v053_timeout_600_and_xiaomiguan_alias_end_to_end(self) -> None:
        # 1. Default 600s timeout + production floor 600s
        default_cfg = AgentConfig()
        self.assertEqual(default_cfg.xiaona_timeout_sec, 600.0)
        env_cfg = AgentConfig.from_env(environ={})
        self.assertEqual(env_cfg.xiaona_timeout_sec, 600.0)

        low_cfg = AgentConfig(
            xiaona_relay_url="https://xiaona-relay.example.com/ask",
            xiaona_relay_key="xn_floor_check",
            xiaona_timeout_sec=120.0,
        )
        seen_timeouts: list[float] = []

        async def capture_timeout_transport(
            url: str, body: bytes, headers: dict[str, str], timeout_sec: float
        ) -> tuple[int, bytes]:
            seen_timeouts.append(timeout_sec)
            return (200, b'{"reply":"ok","media_urls":[]}')

        floor_client = XiaoNaClient(
            low_cfg,
            transport=capture_timeout_transport,
            enable_keychain_fallback=False,
            allow_repo_dotenv_fallback=False,
        )
        await floor_client.ask("ping")
        self.assertEqual(seen_timeouts, [600.0])

        # 2. First-class `小蜜罐` / `小蜜罐🍯` / `xiaomiguan` triggers, reset & query extraction
        for positive in (
            "/小蜜罐",
            "/小蜜罐🍯",
            "/小蜜罐 今天有什么日程",
            "/小蜜罐🍯 帮我总结会议",
            "/xiaomiguan 今天有什么日程",
            "/XiaoMiGuan查周报",
            "@小蜜罐 帮我看下",
            "@xiaomiguan check",
            "小蜜罐：今天有什么安排",
            "小蜜罐🍯：今天有什么安排",
            "问下小蜜罐今天有什么会",
            "让小蜜罐帮我看下录音总结",
            "请小蜜罐回答：重点结论",
            "叫小蜜罐查一下",
            "找小蜜罐核对一下",
            "交给小蜜罐处理",
            "跟小蜜罐说一下明天的安排",
            "小蜜罐帮我看下",
            "/小蜜罐 reset",
            "/xiaomiguan clear",
            "重置小蜜罐会话",
        ):
            self.assertTrue(
                is_explicit_xiaona_request(positive),
                f"Expected explicit XiaoMiGuan trigger for: {positive!r}",
            )
            self.assertTrue(is_xiaomiguan_alias_used(positive))

        for negative in ("/xiaomiguanbot 测试", "myxiaomiguan test", "今天买了一罐蜂蜜"):
            self.assertFalse(
                is_explicit_xiaona_request(negative),
                f"Expected NON-trigger for: {negative!r}",
            )

        for reset_cmd in (
            "/小蜜罐 reset",
            "/小蜜罐 clear",
            "/小蜜罐 重置",
            "/xiaomiguan reset",
            "重置小蜜罐会话",
            "清空小蜜罐会话",
            "重置小蜜罐上下文",
        ):
            self.assertTrue(is_xiaona_reset_command(reset_cmd))

        self.assertEqual(extract_xiaona_query("/小蜜罐"), "")
        self.assertEqual(extract_xiaona_query("/小蜜罐🍯 今天有什么日程"), "今天有什么日程")
        self.assertEqual(extract_xiaona_query("/xiaomiguan 今天有什么日程"), "今天有什么日程")
        self.assertEqual(extract_xiaona_query("问下小蜜罐明天几点开会"), "明天几点开会")
        self.assertEqual(extract_xiaona_query("让小蜜罐回答：核心结论"), "核心结论")

        # 3. Tailored ack + reset + non-admin gate + self-chat `/小蜜罐` / `/xiaomiguan`
        self.assertEqual(
            HyperTextOrchestrator._craft_instant_ack("/小蜜罐 查日程"),
            "收到，我问一下小蜜罐，好了回你。",
        )
        self.assertEqual(
            HyperTextOrchestrator._craft_instant_ack("/xiaomiguan 查日程"),
            "收到，我问一下小蜜罐，好了回你。",
        )
        self.assertEqual(
            HyperTextOrchestrator._craft_instant_ack("/xiaona 查日程"),
            "收到，我问一下小纳，好了回你。",
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            admin = "+8613800138000"
            member = "+8613900001111"
            chat_db = tmp_path / "chat.db"
            _init_self_chat_db(chat_db, admin)

            cfg = AgentConfig(
                provider_mode="deepseek",
                deepseek_api_key="sk-valid-xmg",
                allowed_senders={admin},
                allow_self_chat=True,
                self_chat_prefix="/ai",
                enable_instant_ack=True,
                fast_ack_delay_sec=0.03,
                chat_db_path=chat_db,
                xiaona_relay_url="https://xiaona-relay.example.com/ask",
                xiaona_relay_key="xn_xmg_key",
                state_dir=tmp_path / "state",
            )
            store = StateStore(cfg.sqlite_db_path)
            store.allow_user(member, note="beta")
            mac_bridge = MacIMessageBridge(config=cfg, store=store, human_gate=HumanGate())
            mac_bridge.align_cursor_to_latest()

            conn = sqlite3.connect(str(chat_db))
            for rid, txt, from_me in (
                (1, "/xiaomiguanbot ignore", 1),
                (2, "/xiaomiguanbot ignore", 0),
                (3, "/小蜜罐 查一下周会纪要", 1),
                (4, "/小蜜罐 查一下周会纪要", 0),
                (5, "/xiaomiguan check okr", 1),
                (6, "/xiaomiguan check okr", 0),
            ):
                conn.execute(
                    "INSERT INTO message(ROWID, guid, handle_id, text, is_from_me, associated_message_type) "
                    "VALUES (?, ?, 1, ?, ?, 0)",
                    (rid, f"xmg-{rid}", txt, from_me),
                )
                conn.execute("INSERT INTO chat_message_join(chat_id, message_id) VALUES (1, ?)", (rid,))
            conn.commit()
            conn.close()

            polled = mac_bridge.poll_new_messages()
            self.assertEqual([m.text for m in polled], ["/小蜜罐 查一下周会纪要", "/xiaomiguan check okr"])

            cli_bridge = CliBridge(quiet=True)

            async def slow_xmg_transport(
                url: str, body: bytes, headers: dict[str, str], timeout_sec: float
            ) -> tuple[int, bytes]:
                await asyncio.sleep(0.08)
                return (
                    200,
                    json.dumps({"reply": "小蜜罐已整理好周会纪要。", "media_urls": []}, ensure_ascii=False).encode(
                        "utf-8"
                    ),
                )

            orch = HyperTextOrchestrator(
                config=cfg,
                store=store,
                bridge=cli_bridge,
                human_gate=HumanGate(),
                xiaona_client=XiaoNaClient(
                    cfg,
                    transport=slow_xmg_transport,
                    enable_keychain_fallback=False,
                    allow_repo_dotenv_fallback=False,
                ),
            )

            # Non-admin member blocked on `小蜜罐`
            deny = await orch.handle_inbound_message(
                InboundMessage(
                    rowid=10,
                    guid="xmg-deny",
                    sender=member,
                    text="让小蜜罐查一下日程",
                    timestamp_unix=time.time(),
                )
            )
            self.assertIn("仅限主理人本人调用", deny or "")

            # Admin slow turn gets `"收到，我问一下小蜜罐，好了回你。"` ack + final reply
            cli_bridge.outbox.clear()
            ans = await orch.handle_inbound_message(polled[0])
            self.assertEqual(ans, "小蜜罐已整理好周会纪要。")
            self.assertEqual(cli_bridge.outbox[0][1], "收到，我问一下小蜜罐，好了回你。")
            self.assertEqual(cli_bridge.outbox[1][1], "小蜜罐已整理好周会纪要。")

            # Bare `/小蜜罐` & `/小蜜罐 reset`
            bare_xmg = await orch.handle_inbound_message(
                InboundMessage(rowid=11, guid="xmg-bare", sender=admin, text="/小蜜罐", timestamp_unix=time.time())
            )
            self.assertIn("/小蜜罐 今天有什么日程", bare_xmg or "")
            rst_xmg = await orch.handle_inbound_message(
                InboundMessage(
                    rowid=12, guid="xmg-rst", sender=admin, text="/小蜜罐 reset", timestamp_unix=time.time()
                )
            )
            self.assertIn("已重置小蜜罐会话上下文", rst_xmg or "")
            store.close()

    async def test_9_v053_media_urls_native_attachment_same_host_401_and_25mb_fallbacks(self) -> None:
        import urllib.error
        import urllib.request

        class _FakeHTTPResp:
            def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
                self._buf = io.BytesIO(body)
                self.headers = headers or {}

            def read(self, n: int = -1) -> bytes:
                return self._buf.read(n)

            def close(self) -> None:
                self._buf.close()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            admin = "+8613800138000"
            secret_key = "xn_media_secret_key_777"
            cfg = AgentConfig(
                provider_mode="deepseek",
                deepseek_api_key="sk-valid-media-test",
                allowed_senders={admin},
                enable_instant_ack=False,
                split_standalone_links=True,
                xiaona_relay_url="https://xiaona-relay.example.com/ask",
                xiaona_relay_key=secret_key,
                state_dir=tmp_path,
            )
            store = StateStore(cfg.sqlite_db_path)
            bridge = CliBridge(quiet=True)
            gate = HumanGate()

            opener_calls: list[tuple[str, str | None]] = []

            def fake_url_opener(req: urllib.request.Request, timeout_sec: float) -> Any:
                auth_hdr = req.get_header("Authorization") or req.headers.get("Authorization")
                opener_calls.append((req.full_url, auth_hdr))
                u = req.full_url
                if u.endswith("/same-host-protected.pdf"):
                    if not auth_hdr:
                        raise urllib.error.HTTPError(u, 401, "Unauthorized", {}, None)  # type: ignore[arg-type]
                    return _FakeHTTPResp(
                        b"%PDF-1.4 mock report bytes",
                        {
                            "Content-Type": "application/pdf",
                            "Content-Disposition": "attachment; filename*=UTF-8''%E5%91%A8%E6%8A%A5.pdf",
                        },
                    )
                if "other-cdn.example.com" in u:
                    # Cross-host 401: client MUST NEVER retry with Authorization!
                    raise urllib.error.HTTPError(u, 401, "Unauthorized", {}, None)  # type: ignore[arg-type]
                if u.endswith("/too-large-header.zip"):
                    return _FakeHTTPResp(
                        b"tiny",
                        {"Content-Length": str(26 * 1024 * 1024), "Content-Type": "application/zip"},
                    )
                if u.endswith("/too-large-stream.bin"):
                    # Exceeds custom max_bytes when tested directly or >25MB stream
                    return _FakeHTTPResp(b"x" * (25 * 1024 * 1024 + 1024), {"Content-Type": "application/octet-stream"})
                if u.endswith("/poster.png"):
                    return _FakeHTTPResp(b"\x89PNG\r\n\x1a\nposter-data", {"Content-Type": "image/png"})
                raise urllib.error.HTTPError(u, 404, "Not Found", {}, None)  # type: ignore[arg-type]

            # A) Direct `download_media_url` unit verification:
            #    1) Same-host 401 retries ONCE with Bearer key and saves to state_dir / "xiaona_downloads"
            #    2) Cross-host 401 NEVER sends Authorization header!
            #    3) >25MB Content-Length & >25MB stream abort cleanly and leave zero .tmp_* files!
            xn_client = XiaoNaClient(
                cfg,
                url_opener=fake_url_opener,
                enable_keychain_fallback=False,
                allow_repo_dotenv_fallback=False,
            )

            dl_same = await xn_client.download_media_url(
                "https://xiaona-relay.example.com/files/same-host-protected.pdf"
            )
            self.assertIsNotNone(dl_same)
            assert dl_same is not None
            self.assertEqual(dl_same.parent, tmp_path / "xiaona_downloads")
            self.assertEqual(dl_same.name, "周报.pdf")
            self.assertEqual(
                opener_calls,
                [
                    ("https://xiaona-relay.example.com/files/same-host-protected.pdf", None),
                    (
                        "https://xiaona-relay.example.com/files/same-host-protected.pdf",
                        f"Bearer {secret_key}",
                    ),
                ],
            )

            opener_calls.clear()
            dl_cross = await xn_client.download_media_url(
                "https://other-cdn.example.com/files/private.pdf"
            )
            self.assertIsNone(dl_cross)
            # Strictly 1 unauthenticated call — ZERO Bearer header ever sent to other-cdn.example.com!
            self.assertEqual(
                opener_calls,
                [("https://other-cdn.example.com/files/private.pdf", None)],
            )

            dl_big_hdr = await xn_client.download_media_url(
                "https://xiaona-relay.example.com/files/too-large-header.zip"
            )
            self.assertIsNone(dl_big_hdr)

            dl_big_stream = await xn_client.download_media_url(
                "https://xiaona-relay.example.com/files/too-large-stream.bin"
            )
            self.assertIsNone(dl_big_stream)
            leftover_tmps = list((tmp_path / "xiaona_downloads").glob(".tmp_*"))
            self.assertEqual(leftover_tmps, [])

            # B) End-to-end Orchestrator turn:
            #    - `poster.png` (<=25MB) -> sent as native attachment via `bridge.send_attachment`,
            #      `MEDIA:...` line stripped, and `poster.png` temporary URL NOT spammed in `bridge.outbox`!
            #    - `too-large-header.zip` (>25MB) -> falls back cleanly to URL in `bridge.outbox`!
            async def media_turn_transport(
                url: str, body: bytes, headers: dict[str, str], timeout_sec: float
            ) -> tuple[int, bytes]:
                resp = {
                    "reply": (
                        "海报和大文件包都准备好了：\n"
                        "MEDIA:/tmp/internal_poster.png\n"
                        "https://xiaona-relay.example.com/files/poster.png"
                    ),
                    "media_urls": [
                        "https://xiaona-relay.example.com/files/poster.png",
                        "https://xiaona-relay.example.com/files/too-large-header.zip",
                    ],
                    "session_id": "ext-media",
                    "duration_ms": 540,
                }
                return (200, json.dumps(resp, ensure_ascii=False).encode("utf-8"))

            orch_client = XiaoNaClient(
                cfg,
                transport=media_turn_transport,
                url_opener=fake_url_opener,
                enable_keychain_fallback=False,
                allow_repo_dotenv_fallback=False,
            )
            orch = HyperTextOrchestrator(
                config=cfg,
                store=store,
                bridge=bridge,
                human_gate=gate,
                xiaona_client=orch_client,
            )

            bridge.outbox.clear()
            bridge.attachments.clear()
            ans_media = await orch.handle_inbound_message(
                InboundMessage(
                    rowid=1,
                    guid="med-1",
                    sender=admin,
                    text="/小蜜罐 帮我导出海报和归档包",
                    timestamp_unix=time.time(),
                )
            )
            self.assertIsNotNone(ans_media)
            # Attachment sent for poster.png (and CliBridge.send_attachment returns True)
            self.assertEqual(len(bridge.attachments), 1)
            self.assertTrue(bridge.attachments[0][1].endswith("poster.png"))
            outbox_texts = [txt for _, txt in bridge.outbox]
            joined_outbox = "\n".join(outbox_texts)
            self.assertNotIn("MEDIA:", joined_outbox)
            self.assertNotIn("https://xiaona-relay.example.com/files/poster.png", joined_outbox)
            self.assertIn("https://xiaona-relay.example.com/files/too-large-header.zip", outbox_texts)

            # C) If `bridge.send_attachment` returns False (or raises), fallback sends the URL in text!
            bridge.outbox.clear()
            bridge.attachments.clear()

            async def single_poster_transport(
                url: str, body: bytes, headers: dict[str, str], timeout_sec: float
            ) -> tuple[int, bytes]:
                resp = {
                    "reply": "海报好啦！",
                    "media_urls": ["https://xiaona-relay.example.com/files/poster.png"],
                    "session_id": "ext-media-2",
                    "duration_ms": 200,
                }
                return (200, json.dumps(resp, ensure_ascii=False).encode("utf-8"))

            orch_fallback = HyperTextOrchestrator(
                config=cfg,
                store=store,
                bridge=bridge,
                human_gate=gate,
                xiaona_client=XiaoNaClient(
                    cfg,
                    transport=single_poster_transport,
                    url_opener=fake_url_opener,
                    enable_keychain_fallback=False,
                    allow_repo_dotenv_fallback=False,
                ),
            )
            with mock.patch.object(bridge, "send_attachment", return_value=False):
                await orch_fallback.handle_inbound_message(
                    InboundMessage(
                        rowid=2,
                        guid="med-2",
                        sender=admin,
                        text="/xiaona 再发一次海报",
                        timestamp_unix=time.time(),
                    )
                )
            self.assertEqual(
                [txt for _, txt in bridge.outbox],
                ["海报好啦！", "https://xiaona-relay.example.com/files/poster.png"],
            )
            store.close()


if __name__ == "__main__":
    unittest.main()
