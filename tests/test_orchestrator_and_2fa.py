"""Async unit tests for AgentConfig, DeepSeek/OpenAI routing, HumanGate 2FA, Cron, and Browser-Use 1.0 compliance."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from hypertext import HyperTextOrchestrator, InstinctOrchestrator
from hypertext.bridge_imessage import CliBridge, InboundMessage, MacIMessageBridge
from hypertext.config import AgentConfig, _is_placeholder_key
from hypertext.human_gate import HumanGate
from hypertext.sandbox_browser import BrowserSandbox
from hypertext.selftest import (
    _MockAsyncOpenAI,
    _MockChoice,
    _MockCompletion,
    _MockMessage,
)
from hypertext.store_sqlite import StateStore


class TestOrchestratorAnd2FA(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        self.store = StateStore(self.tmp_dir / "state.sqlite3")
        self.bridge = CliBridge(quiet=True)
        self.gate = HumanGate(default_timeout=3.0)
        self.sender = "+8613800138000"

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_placeholder_api_key_protection_and_default_model(self) -> None:
        self.assertIs(InstinctOrchestrator, HyperTextOrchestrator)
        self.assertTrue(_is_placeholder_key(""))
        self.assertTrue(_is_placeholder_key("sk-替换为你的DeepSeek密钥"))
        self.assertTrue(_is_placeholder_key("sk-你的密钥"))
        self.assertTrue(_is_placeholder_key("your_api_key_here"))
        self.assertTrue(_is_placeholder_key("sk-含中文非ASCII字符"))
        self.assertFalse(_is_placeholder_key("sk-9a8b7c6d5e4f3a2b1c0d"))

        cfg = AgentConfig(
            provider_mode="deepseek",
            deepseek_api_key="sk-替换为你的DeepSeek密钥",
            openai_api_key="sk-real-openai-fallback-key",
            state_dir=self.tmp_dir,
        )
        self.assertEqual(cfg.deepseek_model, "deepseek-flash")
        self.assertEqual(cfg.deepseek_api_key, "")
        self.assertEqual(cfg.deepseek_key_status, "placeholder")
        prov, key, _, is_ds = cfg.resolve_active_provider()
        self.assertEqual(prov, "openai")
        self.assertEqual(key, "sk-real-openai-fallback-key")
        self.assertFalse(is_ds)

    async def test_deepseek_thinking_disabled_when_effort_none(self) -> None:
        async def handler(kwargs: dict[str, Any], call_num: int) -> _MockCompletion:
            return _MockCompletion(
                choices=[_MockChoice(message=_MockMessage(content="极速无思考回复", reasoning_content=""))]
            )

        client = _MockAsyncOpenAI(handler)
        cfg = AgentConfig(
            provider_mode="deepseek",
            deepseek_api_key="sk-valid-deepseek-12345",
            deepseek_reasoning_effort="none",
            enable_deepseek_web_tools=True,
            allowed_senders={self.sender},
            state_dir=self.tmp_dir,
        )
        orch = HyperTextOrchestrator(
            config=cfg,
            store=self.store,
            bridge=self.bridge,
            human_gate=self.gate,
            llm_client=client,
        )
        await orch.handle_inbound_message(
            InboundMessage(rowid=1, guid="g1", sender=self.sender, text="你好", timestamp_unix=time.time())
        )
        sent_req = client.chat.completions.calls[0]
        self.assertEqual(sent_req["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertNotIn("reasoning_effort", sent_req)

    async def test_active_member_does_not_inherit_admin_with_empty_allowlist(self) -> None:
        self.store.allow_user(self.sender)
        config = AgentConfig(state_dir=self.tmp_dir, enable_invite_codes=False)
        orch = HyperTextOrchestrator(
            config=config, store=self.store, bridge=self.bridge, human_gate=self.gate,
        )
        with patch.object(orch, "_run_agent_turn", side_effect=AssertionError("Unexpected turn")):
            reply = await orch.handle_inbound_message(
                InboundMessage(1, "member", self.sender, "/codex review", time.time())
            )
        self.assertIn("仅限主理人本人", reply)
        self.assertTrue(orch._is_admin_sender("local-cli-user"))
        orch.bridge = MacIMessageBridge(store=self.store, config=config)
        self.assertFalse(orch._is_admin_sender("local-cli-user"))

    async def test_stop_removes_inbox_request_before_acknowledging(self) -> None:
        config = AgentConfig(allowed_senders={self.sender}, state_dir=self.tmp_dir)
        bridge = MacIMessageBridge(store=self.store, config=config, durable_inbox=True)
        orch = HyperTextOrchestrator(
            config=config, store=self.store, bridge=bridge, human_gate=self.gate,
        )
        start = asyncio.Event()
        release = asyncio.Event()
        self.store.record_imessage_batch(bridge.CURSOR_META_KEY, 1, [(1, "{}")])
        self.store.start_imessage(1)

        async def long_turn(**_kwargs):
            start.set()
            try:
                await asyncio.Future()
            finally:
                await release.wait()

        async def send_text(_recipient, _text):
            self.assertEqual(self.store.pending_imessages(), [])

        with patch.object(orch, "_run_agent_turn", side_effect=long_turn), patch.object(
            bridge, "send_text", side_effect=send_text
        ):
            turn = asyncio.create_task(orch.handle_inbound_message(
                InboundMessage(1, "turn", self.sender, "等待任务完成", time.time())
            ))
            try:
                await asyncio.wait_for(start.wait(), timeout=2)
                reply = await orch.handle_inbound_message(
                    InboundMessage(2, "stop", self.sender, "停", time.time())
                )
                self.assertIn("已经停下", reply)
            finally:
                release.set()
                await asyncio.wait_for(turn, timeout=2)

    async def test_failed_or_cancelled_approval_send_clears_pending_gate(self) -> None:
        with patch.object(self.bridge, "send_text", side_effect=RuntimeError("delivery failed")):
            with self.assertRaises(RuntimeError):
                await self.gate.request_input(self.sender, "approval", bridge=self.bridge)
        self.assertFalse(self.gate.has_pending(self.sender))

        sending = asyncio.Event()

        async def blocked_send(_recipient, _question):
            sending.set()
            await asyncio.Future()

        with patch.object(self.bridge, "send_text", side_effect=blocked_send):
            request = asyncio.create_task(
                self.gate.request_input(self.sender, "approval", bridge=self.bridge)
            )
            await asyncio.wait_for(sending.wait(), timeout=1)
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request
        self.assertFalse(self.gate.has_pending(self.sender))

    async def test_controls_reach_older_turn_after_newer_turn_finishes(self) -> None:
        cfg = AgentConfig(
            allowed_senders={self.sender}, state_dir=self.tmp_dir, enable_instant_ack=False,
        )
        orch = HyperTextOrchestrator(
            config=cfg, store=self.store, bridge=self.bridge, human_gate=self.gate,
        )
        waiting = asyncio.Event()

        async def run_turn(**kwargs):
            if kwargs["user_text"] == "原来的任务":
                waiting.set()
                return await self.gate.request_input(self.sender, "approval")
            return "新任务已完成"

        def msg(rowid, text):
            return InboundMessage(rowid, str(rowid), self.sender, text, time.time())

        with patch.object(orch, "_run_agent_turn", side_effect=run_turn):
            first = asyncio.create_task(orch.handle_inbound_message(msg(1, "原来的任务")))
            try:
                await asyncio.wait_for(waiting.wait(), timeout=1)
                second = await orch.handle_inbound_message(msg(2, "新的普通任务"))
                self.assertEqual(second, "新任务已完成")
                status = await orch.handle_inbound_message(msg(3, "/status"))
                self.assertIn("原来的任务", status)
                self.assertIn("等你确认", status)
                self.assertTrue(self.gate.has_pending(self.sender))
                stopped = await orch.handle_inbound_message(msg(4, "/stop"))
                self.assertIn("已经停下", stopped)
                self.assertIsNone(await asyncio.wait_for(first, timeout=1))
                self.assertFalse(self.gate.has_pending(self.sender))
            finally:
                if not first.done():
                    first.cancel()
                    await asyncio.gather(first, return_exceptions=True)

    async def test_revoked_user_cannot_redeem_or_run_scheduled_work(self) -> None:
        cfg = AgentConfig(state_dir=self.tmp_dir, enable_invite_codes=True)
        orch = HyperTextOrchestrator(
            config=cfg, store=self.store, bridge=self.bridge, human_gate=self.gate,
        )
        code = self.store.create_invite_code(code="HT-TESTCODE", max_uses=3)
        self.assertTrue(self.store.redeem_invite_code(self.sender, code)[0])
        now = time.time()
        job = self.store.add_scheduled_task(self.sender, "agent_wakeup", "task", now, 60)
        self.store.revoke_user(self.sender)
        self.assertFalse(self.store.cancel_scheduled_task(job, sender=self.sender))
        self.assertFalse(self.store.redeem_invite_code(self.sender, code)[0])
        self.assertEqual(self.store.list_invite_codes()[0]["used_count"], 1)

        # A pre-upgrade recurring task may remain enabled even though its owner was revoked.
        legacy_job = self.store.add_scheduled_task(self.sender, "agent_wakeup", "old", now, 60)
        with patch.object(orch, "_run_agent_turn", new_callable=AsyncMock) as run_turn:
            self.assertIsNone(await orch.handle_inbound_message(
                InboundMessage(1, "revoked", self.sender, code, now)
            ))
            self.assertEqual(await orch.process_due_tasks_once(now_ts=now + 1), 0)
            run_turn.assert_not_awaited()
        self.assertFalse(self.store.cancel_scheduled_task(legacy_job, sender=self.sender))
        self.assertEqual(self.bridge.outbox, [])

    async def test_runtime_fallback_strips_deepseek_extensions(self) -> None:
        self.store.append_conversation_message(self.sender, "user", "历史问题")
        self.store.append_conversation_message(
            self.sender,
            "assistant",
            "历史回答",
            reasoning_content="历史思考链",
            server_tool_calls=[{"id": "s1", "type": "deepseek_web_search"}],
        )

        async def failing_ds(kwargs: dict[str, Any], call_num: int) -> _MockCompletion:
            raise RuntimeError("DeepSeek upstream 503")

        async def working_oa(kwargs: dict[str, Any], call_num: int) -> _MockCompletion:
            return _MockCompletion(
                choices=[_MockChoice(message=_MockMessage(content="备用通道成功接管"))]
            )

        ds_client = _MockAsyncOpenAI(failing_ds)
        oa_client = _MockAsyncOpenAI(working_oa)
        cfg = AgentConfig(
            provider_mode="deepseek",
            deepseek_api_key="sk-valid-ds-key",
            openai_api_key="sk-valid-oa-key",
            allowed_senders={self.sender},
            state_dir=self.tmp_dir,
        )
        orch = HyperTextOrchestrator(
            config=cfg,
            store=self.store,
            bridge=self.bridge,
            human_gate=self.gate,
            llm_client=ds_client,
            fallback_llm_client=oa_client,
        )
        reply = await orch.handle_inbound_message(
            InboundMessage(rowid=2, guid="g2", sender=self.sender, text="新问题", timestamp_unix=time.time())
        )
        self.assertEqual(reply, "备用通道成功接管")
        oa_req = oa_client.chat.completions.calls[0]
        self.assertNotIn("extra_body", oa_req)
        self.assertNotIn("reasoning_effort", oa_req)
        for m in oa_req["messages"]:
            self.assertNotIn("reasoning_content", m)
            self.assertNotIn("server_tool_calls", m)
        fn_names = [
            t["function"]["name"] for t in oa_req["tools"] if t.get("type") == "function"
        ]
        self.assertIn("fetch_url_text", fn_names)

    async def test_browser_sandbox_1_0_compliance_and_2fa_action(self) -> None:
        browser_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        chat_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        registered_actions: dict[str, tuple[str, Any]] = {}

        class FakeBrowser:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                if not args and not kwargs:
                    raise AssertionError("Zero-argument cloud browser invocation is strictly forbidden!")
                browser_calls.append((args, kwargs))
                self.stopped = False

            async def stop(self) -> None:
                self.stopped = True

        class FakeChatOpenAI:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                chat_calls.append((args, kwargs))

        class FakeTools:
            def action(self, description: str) -> Any:
                if not isinstance(description, str) or not description.strip():
                    raise AssertionError("@tools.action must take an explicit description string!")

                def decorator(fn: Any) -> Any:
                    registered_actions[fn.__name__] = (description, fn)
                    return fn

                return decorator

        class FakeHistory:
            def final_result(self) -> str:
                return "已成功完成值机选座 12F（靠窗）。"

        class FakeBrowserAgent:
            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs

            async def run(self, max_steps: int = 25) -> FakeHistory:
                _, ask_fn = registered_actions["ask_human_via_imessage"]
                ans = await ask_fn("请输入航司短信验证码")
                assert "654321" in ans
                return FakeHistory()

        fake_bu_mod = types.ModuleType("browser_use")
        fake_bu_mod.Agent = FakeBrowserAgent  # type: ignore[attr-defined]
        fake_bu_mod.Browser = FakeBrowser  # type: ignore[attr-defined]
        fake_bu_mod.ChatOpenAI = FakeChatOpenAI  # type: ignore[attr-defined]
        fake_bu_mod.Tools = FakeTools  # type: ignore[attr-defined]

        cfg = AgentConfig(
            provider_mode="deepseek",
            deepseek_api_key="sk-valid-ds-browser-key",
            deepseek_base_url="https://api.deepseek.com",
            deepseek_model="deepseek-flash",
            browser_headless=False,
            state_dir=self.tmp_dir,
        )
        sandbox = BrowserSandbox(config=cfg, human_gate=self.gate, bridge=self.bridge)

        with patch.dict(sys.modules, {"browser_use": fake_bu_mod}):
            task = asyncio.create_task(sandbox.run_task(self.sender, "办理东航值机靠窗座位"))
            for _ in range(50):
                if self.gate.has_pending(self.sender):
                    break
                await asyncio.sleep(0.02)
            self.assertTrue(self.gate.has_pending(self.sender))
            self.assertTrue(self.gate.try_resolve(self.sender, "654321"))
            result = await asyncio.wait_for(task, timeout=2.0)

        self.assertIn("12F", result)
        self.assertEqual(browser_calls[0][0], ("chromium",))
        self.assertEqual(browser_calls[0][1]["headless"], False)
        self.assertEqual(
            chat_calls[0][1]["base_url"], "https://api.deepseek.com/v1"
        )


if __name__ == "__main__":
    unittest.main()
