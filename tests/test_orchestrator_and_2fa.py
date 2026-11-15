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
from unittest.mock import patch

from instinct_mac.bridge_imessage import CliBridge, InboundMessage
from instinct_mac.config import AgentConfig, _is_placeholder_key
from instinct_mac.human_gate import HumanGate
from instinct_mac.orchestrator import InstinctOrchestrator
from instinct_mac.sandbox_browser import BrowserSandbox
from instinct_mac.selftest import (
    _MockAsyncOpenAI,
    _MockChoice,
    _MockCompletion,
    _MockMessage,
)
from instinct_mac.store_sqlite import StateStore


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
            state_dir=self.tmp_dir,
        )
        orch = InstinctOrchestrator(
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
            state_dir=self.tmp_dir,
        )
        orch = InstinctOrchestrator(
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
