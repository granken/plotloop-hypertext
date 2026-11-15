"""Unit tests for HyperText v0.5.1 UX: Zero-Markdown formatter, HTML cards, Instant Ack, Live Interruption, Attachments & Hot Whitelist."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import pathlib
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from hypertext import (
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
    format_for_imessage,
    is_sensitive_attachment_path,
    render_mobile_html_card,
)
from hypertext.cli import main as cli_main
from hypertext.selftest import (
    _MockAsyncOpenAI,
    _MockChoice,
    _MockCompletion,
    _MockFunction,
    _MockMessage,
    _MockToolCall,
)


def _msg(
    rowid: int,
    sender: str,
    text: str,
    images: list[str] | None = None,
) -> InboundMessage:
    return InboundMessage(
        rowid=rowid,
        guid=f"xn-{rowid}",
        sender=sender,
        text=text,
        timestamp_unix=1700000000.0 + rowid,
        image_data_urls=list(images or []),
        is_from_me=0,
    )


class TestXiaoNaUXAndAttachments(unittest.IsolatedAsyncioTestCase):
    def test_format_for_imessage_zero_markdown_rich_links_and_math_preservation(self) -> None:
        raw = (
            "# 东京周末出行建议\n\n"
            "> 已按你的靠窗偏好筛选\n\n"
            "**首选航班**：东航 `MU537`，含税 *2180元*，尺寸计算 3 * 4 = 12，分辨率 1920*1080。\n"
            "~~已售罄备选~~\n"
            "---\n"
            "| 车次/航班 | 出发时间 | 票价 |\n"
            "| :--- | :---: | ---: |\n"
            "| **MU537** | 09:15 | `¥2180` |\n"
            "| NH920 | 13:40 | ¥2650 |\n\n"
            "- 航司官网预订入口：[东航值机与选座](https://ceair.example.com/book?flight=MU537&seat=window#confirm)\n"
            "- 维基参考：(https://en.wikipedia.org/wiki/Apple_(company))。\n"
            "- 纯链接去重：[https://status.example.com/live_check](https://status.example.com/live_check)\n\n"
            "```python\n"
            "confirmation_code = 'XK92M1'\n"
            "```\n"
        )
        out = format_for_imessage(raw)

        # Zero raw Markdown tokens
        self.assertFalse("**" in out)
        self.assertFalse("###" in out or out.startswith("# "))
        self.assertFalse("```" in out)
        self.assertFalse("`MU537`" in out)
        self.assertFalse("~~" in out)
        self.assertFalse("| :---" in out)

        # Math & resolution multiplication preserved
        self.assertIn("3 * 4 = 12", out)
        self.assertIn("1920*1080", out)

        # Table converted to mobile bullet lines
        self.assertIn("· 车次/航班：MU537 ｜ 出发时间：09:15 ｜ 票价：¥2180", out)
        self.assertIn("· 车次/航班：NH920 ｜ 出发时间：13:40 ｜ 票价：¥2650", out)

        # Code block inner text preserved without fences
        self.assertIn("confirmation_code = 'XK92M1'", out)

        # Every URL stands cleanly on its own line for iMessage Rich Link bubble rendering
        lines = [ln.strip() for ln in out.splitlines()]
        self.assertIn("东航值机与选座", out)
        self.assertIn(
            "https://ceair.example.com/book?flight=MU537&seat=window#confirm",
            lines,
        )
        self.assertIn("https://en.wikipedia.org/wiki/Apple_(company)", lines)
        self.assertIn("https://status.example.com/live_check", lines)
        self.assertEqual(out.count("https://status.example.com/live_check"), 1)

    def test_render_mobile_html_card_xss_safe_and_responsive(self) -> None:
        malicious_title = '早报 <script>alert("xss")</script>'
        body = (
            "## 市场速览\n"
            "危险标签：<img src=x onerror=alert(1)> 与 [伪造脚本](javascript:alert(2))\n\n"
            "- 正常来源：[DeepSeek 官网](https://www.deepseek.com/news?id=1&lang=zh)\n\n"
            "| 指标 | 数值 |\n"
            "| --- | --- |\n"
            "| 吞吐量 | **120 tok/s** |\n"
        )
        card = render_mobile_html_card(
            title=malicious_title,
            body=body,
            source_links=["https://arxiv.org/abs/2501.00001"],
        )
        self.assertIn("<!DOCTYPE html>", card)
        self.assertIn('name="viewport"', card)
        self.assertIn("prefers-color-scheme: dark", card)
        self.assertIn("-apple-system", card)

        # XSS neutralized
        self.assertFalse("<script>" in card)
        self.assertFalse("<img src=x" in card)
        self.assertFalse('href="javascript:' in card)
        self.assertIn("&lt;script&gt;", card)

        # Valid links & responsive table rendered
        self.assertIn(
            'href="https://www.deepseek.com/news?id=1&amp;lang=zh"',
            card,
        )
        self.assertIn('href="https://arxiv.org/abs/2501.00001"', card)
        self.assertIn("<table>", card)
        self.assertIn("<strong>120 tok/s</strong>", card)

    async def test_instant_ack_intents_fast_suppression_slow_delivery_and_no_duplicate_browser_banner(self) -> None:
        # 1. Direct intent crafting checks
        self.assertIn(
            "打开页面去办",
            HyperTextOrchestrator._craft_instant_ack("帮我打开东航网页办理值机", False),
        )
        self.assertIn(
            "点开这个链接",
            HyperTextOrchestrator._craft_instant_ack("看看 https://example.com/post 讲了啥", False),
        )
        self.assertEqual(
            HyperTextOrchestrator._craft_instant_ack("", True),
            "收到图了，我看一下哈。",
        )
        self.assertIn(
            "查一下最新情况",
            HyperTextOrchestrator._craft_instant_ack("查一下明天上海天气", False),
        )
        self.assertEqual(
            HyperTextOrchestrator._craft_instant_ack("帮我把这段话润色得更自然得体一点", False),
            "收到，我处理一下马上回你。",
        )
        self.assertEqual(HyperTextOrchestrator._craft_instant_ack("早安", False), "")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = StateStore(tmp_path / "state.db")
            bridge = CliBridge(quiet=True)
            gate = HumanGate()
            sender = "+8613800138000"

            # 2. Fast turn (< fast_ack_delay_sec) -> NO redundant ack sent!
            async def fast_llm(kwargs: dict[str, Any], call_num: int) -> _MockCompletion:
                return _MockCompletion(
                    choices=[_MockChoice(message=_MockMessage(content="**上海晴**，22度。"))]
                )

            cfg_fast = AgentConfig(
                provider_mode="deepseek",
                deepseek_api_key="sk-valid-key-fast",
                allowed_senders={sender},
                enable_instant_ack=True,
                fast_ack_delay_sec=0.3,
                state_dir=tmp_path,
            )
            orch_fast = HyperTextOrchestrator(
                config=cfg_fast,
                store=store,
                bridge=bridge,
                human_gate=gate,
                llm_client=_MockAsyncOpenAI(fast_llm),
            )
            ans_fast = await orch_fast.handle_inbound_message(
                _msg(1, sender, "查一下上海今天天气怎么样")
            )
            self.assertEqual(ans_fast, "上海晴，22度。")
            self.assertEqual(len(bridge.outbox), 1)
            self.assertEqual(bridge.outbox[0][1], "上海晴，22度。")

            # 3. Slow browser turn (> fast_ack_delay_sec) -> 1 natural ack BEFORE final reply, ZERO duplicate browser banner!
            bridge.outbox.clear()

            async def slow_browser_llm(kwargs: dict[str, Any], call_num: int) -> _MockCompletion:
                if call_num == 1:
                    await asyncio.sleep(0.08)
                    return _MockCompletion(
                        choices=[
                            _MockChoice(
                                message=_MockMessage(
                                    content="",
                                    tool_calls=[
                                        _MockToolCall(
                                            id="tc_b1",
                                            function=_MockFunction(
                                                name="run_browser_task",
                                                arguments=json.dumps({"task_prompt": "打开网页查余票"}),
                                            ),
                                        )
                                    ],
                                )
                            )
                        ]
                    )
                return _MockCompletion(
                    choices=[_MockChoice(message=_MockMessage(content="已查妥，还有 **8** 张余票。"))]
                )

            fake_sandbox = mock.MagicMock(spec=BrowserSandbox)

            async def fake_run_task(**kwargs: Any) -> str:
                await asyncio.sleep(0.08)
                return "余票 8 张"

            fake_sandbox.run_task = mock.AsyncMock(side_effect=fake_run_task)

            cfg_slow = AgentConfig(
                provider_mode="deepseek",
                deepseek_api_key="sk-valid-key-slow",
                allowed_senders={sender},
                enable_instant_ack=True,
                fast_ack_delay_sec=0.03,
                state_dir=tmp_path,
            )
            orch_slow = HyperTextOrchestrator(
                config=cfg_slow,
                store=store,
                bridge=bridge,
                human_gate=gate,
                browser_sandbox=fake_sandbox,
                llm_client=_MockAsyncOpenAI(slow_browser_llm),
            )
            ans_slow = await orch_slow.handle_inbound_message(
                _msg(2, sender, "帮我打开网页查一下今晚去杭州余票")
            )
            self.assertEqual(ans_slow, "已查妥，还有 8 张余票。")
            # Exactly 2 messages: [0] natural browser ack, [1] clean final reply (zero duplicate banner!)
            self.assertEqual(len(bridge.outbox), 2)
            self.assertIn("打开页面去办", bridge.outbox[0][1])
            self.assertEqual(bridge.outbox[1][1], "已查妥，还有 8 张余票。")
            store.close()

    async def test_live_interruption_status_stop_and_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = StateStore(tmp_path / "state.db")
            bridge = CliBridge(quiet=True)
            gate = HumanGate()
            sender = "+8613800138000"

            entered = asyncio.Event()

            async def long_running_llm(kwargs: dict[str, Any], call_num: int) -> _MockCompletion:
                entered.set()
                await asyncio.sleep(5.0)
                return _MockCompletion(
                    choices=[_MockChoice(message=_MockMessage(content="不会到达这里"))]
                )

            cfg = AgentConfig(
                provider_mode="deepseek",
                deepseek_api_key="sk-valid-long-key",
                allowed_senders={sender},
                enable_instant_ack=False,
                heartbeat_interval_sec=0.06,
                state_dir=tmp_path,
            )
            orch = HyperTextOrchestrator(
                config=cfg,
                store=store,
                bridge=bridge,
                human_gate=gate,
                llm_client=_MockAsyncOpenAI(long_running_llm),
            )

            bg_task = asyncio.create_task(
                orch.handle_inbound_message(_msg(1, sender, "帮我深度调研全球人形机器人供应链格局"))
            )
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            await asyncio.sleep(0.09)

            # Calm single-cap reassurance fired once
            self.assertTrue(
                any(
                    text == "内容稍微有点多，还在做最后的核对整理，马上好。"
                    for _, text in bridge.outbox
                ),
                f"Expected calm v0.5.1 reassurance in outbox, got: {bridge.outbox}",
            )

            # Live progress check returns immediately without blocking!
            status_res = await asyncio.wait_for(
                orch.handle_inbound_message(_msg(2, sender, "在干嘛？")),
                timeout=0.3,
            )
            self.assertIsNotNone(status_res)
            assert status_res is not None
            self.assertIn("还在办「", status_res)
            self.assertFalse(bg_task.done())

            # Live stop command cancels active task immediately!
            stop_res = await asyncio.wait_for(
                orch.handle_inbound_message(_msg(3, sender, "停一下")),
                timeout=0.3,
            )
            self.assertEqual(stop_res, "好，刚才那件事已经停下了。有新安排你随时说。")
            bg_outcome = await asyncio.wait_for(bg_task, timeout=0.5)
            self.assertIsNone(bg_outcome)
            store.close()

    async def test_document_card_delivery_attachment_security_and_mac_bridge_osascript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = StateStore(tmp_path / "state.db")
            bridge = CliBridge(quiet=True)
            gate = HumanGate()
            sender = "+8613800138000"

            safe_csv = tmp_path / "导出数据.csv"
            safe_csv.write_text("日期,营收\n11-15,12800\n", encoding="utf-8")
            secret_env = tmp_path / ".env"
            secret_env.write_text("DEEPSEEK_API_KEY=sk-top-secret", encoding="utf-8")

            tool_results_seen: list[dict[str, Any]] = []

            async def tool_driving_llm(kwargs: dict[str, Any], call_num: int) -> _MockCompletion:
                if call_num == 1:
                    return _MockCompletion(
                        choices=[
                            _MockChoice(
                                message=_MockMessage(
                                    content="",
                                    tool_calls=[
                                        _MockToolCall(
                                            id="tc_doc",
                                            function=_MockFunction(
                                                name="create_and_send_document",
                                                arguments=json.dumps(
                                                    {
                                                        "title": "AI早报",
                                                        "content": "# 重点\n- 新模型发布\nhttps://example.com/ai",
                                                        "doc_format": "html",
                                                        "filename": "AI早报_1115.html",
                                                        "caption": "**早报卡片**已生成",
                                                    },
                                                    ensure_ascii=False,
                                                ),
                                            ),
                                        ),
                                        _MockToolCall(
                                            id="tc_steal_env",
                                            function=_MockFunction(
                                                name="send_local_attachment",
                                                arguments=json.dumps({"file_path": str(secret_env)}),
                                            ),
                                        ),
                                        _MockToolCall(
                                            id="tc_steal_db",
                                            function=_MockFunction(
                                                name="send_local_attachment",
                                                arguments=json.dumps(
                                                    {"file_path": str(tmp_path / "state.db")}
                                                ),
                                            ),
                                        ),
                                        _MockToolCall(
                                            id="tc_safe_csv",
                                            function=_MockFunction(
                                                name="send_local_attachment",
                                                arguments=json.dumps(
                                                    {"file_path": str(safe_csv), "caption": "附表"}
                                                ),
                                            ),
                                        ),
                                    ],
                                )
                            )
                        ]
                    )
                for m in kwargs["messages"]:
                    if m.get("role") == "tool":
                        tool_results_seen.append(json.loads(m["content"]))
                return _MockCompletion(
                    choices=[
                        _MockChoice(
                            message=_MockMessage(
                                content="卡片和附表都发你啦。\nhttps://example.com/ai"
                            )
                        )
                    ]
                )

            cfg = AgentConfig(
                provider_mode="deepseek",
                deepseek_api_key="sk-valid-doc-key",
                allowed_senders={sender},
                enable_instant_ack=False,
                state_dir=tmp_path,
            )
            orch = HyperTextOrchestrator(
                config=cfg,
                store=store,
                bridge=bridge,
                human_gate=gate,
                llm_client=_MockAsyncOpenAI(tool_driving_llm),
            )

            final_text = await orch.handle_inbound_message(
                _msg(1, sender, "给我做一份详细的今日AI早报并附上CSV")
            )
            self.assertIn("https://example.com/ai", final_text or "")

            # Verify .env and state.db were blocked while HTML card and safe CSV succeeded
            self.assertEqual(len(tool_results_seen), 4)
            self.assertTrue(tool_results_seen[0]["ok"])
            self.assertFalse(tool_results_seen[1]["ok"])  # .env blocked!
            self.assertFalse(tool_results_seen[2]["ok"])  # state.db blocked!
            self.assertTrue(tool_results_seen[3]["ok"])   # safe CSV allowed!

            self.assertEqual(len(bridge.attachments), 2)
            card_path = Path(bridge.attachments[0][1])
            self.assertEqual(card_path.name, "AI早报_1115.html")
            self.assertIn("<!DOCTYPE html>", card_path.read_text(encoding="utf-8"))

            # Verify MacIMessageBridge.send_attachment stages to outbox & calls parameterized osascript
            chat_db = tmp_path / "chat.db"
            chat_db.write_bytes(b"")
            mac_bridge = MacIMessageBridge(config=cfg, store=store)
            fake_home = tmp_path / "fake_home"
            fake_home.mkdir()

            proc_mock = mock.AsyncMock()
            proc_mock.communicate = mock.AsyncMock(return_value=(b"", b""))
            proc_mock.returncode = 0

            with (
                mock.patch.object(pathlib.Path, "home", return_value=fake_home),
                mock.patch("asyncio.create_subprocess_exec", return_value=proc_mock) as exec_mock,
            ):
                await mac_bridge.send_attachment(
                    sender, card_path, caption="**今日早报**请查收"
                )

            # Call 1: attachment AppleScript; Call 2: formatted caption with BOT_ECHO_MARKER
            self.assertEqual(exec_mock.call_count, 2)
            att_call_args = exec_mock.call_args_list[0][0]
            self.assertIn("POSIX file filePath", att_call_args[2])
            self.assertEqual(att_call_args[3], sender)
            self.assertTrue(att_call_args[4].endswith("PlotLoopOutbox/AI早报_1115.html"))
            self.assertTrue(Path(att_call_args[4]).is_file())

            cap_call_args = exec_mock.call_args_list[1][0]
            self.assertEqual(cap_call_args[4], f"{BOT_ECHO_MARKER}今日早报请查收")

            # Also verify BrowserSandbox screenshot action via fake Browser.take_screenshot
            registered_actions: dict[str, Any] = {}

            class FakeBrowser:
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    pass

                async def take_screenshot(self, path: str | None = None) -> bytes:
                    png_bytes = b"\x89PNG\r\n\x1a\nshotbytes"
                    if path:
                        Path(path).write_bytes(png_bytes)
                    return png_bytes

                async def stop(self) -> None:
                    return None

            class FakeTools:
                def action(self, desc: str) -> Any:
                    def _dec(fn: Any) -> Any:
                        registered_actions[fn.__name__] = fn
                        return fn
                    return _dec

            class FakeBrowserAgent:
                def __init__(self, **kwargs: Any) -> None:
                    pass

                async def run(self, max_steps: int = 25) -> Any:
                    shot_fn = registered_actions["send_screenshot_or_file_via_imessage"]
                    res_txt = await shot_fn("**选座确认页**")
                    assert "Screenshot" in res_txt
                    return types.SimpleNamespace(final_result=lambda: "截图已回传")

            fake_bu = types.ModuleType("browser_use")
            fake_bu.Agent = FakeBrowserAgent  # type: ignore[attr-defined]
            fake_bu.Browser = FakeBrowser  # type: ignore[attr-defined]
            fake_bu.ChatOpenAI = lambda *a, **kw: None  # type: ignore[attr-defined]
            fake_bu.Tools = FakeTools  # type: ignore[attr-defined]

            bridge.attachments.clear()
            sandbox = BrowserSandbox(config=cfg, human_gate=gate, bridge=bridge)
            with mock.patch.dict(sys.modules, {"browser_use": fake_bu}):
                b_out = await sandbox.run_task(sender, "截图发我确认")
            self.assertEqual(b_out, "截图已回传")
            self.assertEqual(len(bridge.attachments), 1)
            self.assertEqual(bridge.attachments[0][2], "选座确认页")
            store.close()

    async def test_cli_allow_user_and_revoke_user_hot_effective(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            env_file = tmp_path / "xiaona.env"
            env_file.write_text(
                "\n".join(
                    [
                        "PROVIDER_MODE=deepseek",
                        "DEEPSEEK_MODEL=deepseek-flash",
                        f"STATE_DIR={state_dir}",
                        "ALLOWED_SENDERS=+8613800138000",
                        "ENABLE_INVITE_CODES=false",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            cfg = AgentConfig.from_env(env_file=env_file)
            cfg.deepseek_api_key = "sk-valid-hot-auth-key"
            store = StateStore(cfg.sqlite_db_path)
            bridge = CliBridge(quiet=True)
            gate = HumanGate()

            async def echo_llm(kwargs: dict[str, Any], call_num: int) -> _MockCompletion:
                return _MockCompletion(
                    choices=[_MockChoice(message=_MockMessage(content="你好呀，欢迎使用 HyperText！"))]
                )

            self.assertIs(InstinctOrchestrator, HyperTextOrchestrator)
            orch = HyperTextOrchestrator(
                config=cfg,
                store=store,
                bridge=bridge,
                human_gate=gate,
                llm_client=_MockAsyncOpenAI(echo_llm),
            )

            guest = "+86 135-0000-7777"
            # Initially unauthorized (ENABLE_INVITE_CODES=false -> silently ignored)
            r0 = await orch.handle_inbound_message(_msg(1, guest, "在吗"))
            self.assertIsNone(r0)

            # Hot whitelist via CLI --allow-user WITHOUT restarting orchestrator!
            out_allow = io.StringIO()
            with contextlib.redirect_stdout(out_allow):
                rc_allow = cli_main(
                    [
                        "--env-file",
                        str(env_file),
                        "--allow-user",
                        guest,
                        "--user-note",
                        "老朋友体验",
                    ]
                )
            self.assertEqual(rc_allow, 0)
            self.assertTrue(store.is_authorized_user(guest))

            r1 = await orch.handle_inbound_message(_msg(2, guest, "现在可以聊了吗"))
            self.assertEqual(r1, "你好呀，欢迎使用 HyperText！")

            # Hot revoke via CLI --revoke-user -> immediately blocked without restart!
            out_revoke = io.StringIO()
            with contextlib.redirect_stdout(out_revoke):
                rc_revoke = cli_main(
                    ["--env-file", str(env_file), "--revoke-user", guest]
                )
            self.assertEqual(rc_revoke, 0)
            self.assertFalse(store.is_authorized_user(guest))
            self.assertTrue(store.is_revoked_user(guest))

            r2 = await orch.handle_inbound_message(_msg(3, guest, "还能发吗"))
            self.assertIsNone(r2)
            store.close()


if __name__ == "__main__":
    unittest.main()
