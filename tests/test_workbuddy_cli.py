"""Comprehensive unit tests for HyperText v0.5.4 Explicit-Only Admin WorkBuddy CLI integration."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import sqlite3
import stat
import tempfile
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hypertext import (
    BOT_ECHO_MARKER,
    AgentConfig,
    CliBridge,
    HumanGate,
    HyperTextOrchestrator,
    InboundMessage,
    MacIMessageBridge,
    StateStore,
    WorkBuddyClient,
    XiaoNaClient,
    build_workbuddy_child_env,
    discover_workbuddy_cli,
    extract_workbuddy_query,
    is_compound_workbuddy_request,
    is_compound_xiaona_request,
    is_explicit_workbuddy_request,
    is_workbuddy_reset_command,
    parse_workbuddy_output,
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


class TestWorkBuddyCLIIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_1_triggers_query_extraction_compound_and_zero_implicit_exposure(self) -> None:
        for positive in (
            "/wb",
            "/workbuddy",
            "/codebuddy",
            "/wb 帮我写个HTML汇报卡片",
            "/WB生成竞品对比表",
            "/workbuddy 分析这段日志",
            "/codebuddy: fix script",
            "@wb 帮我看下",
            "@workbuddy 生成PPT",
            "@codebuddy check",
            "让workbuddy查一下",
            "让 workbuddy 帮我做一份竞品对比表",
            "问下 workbuddy 这个报错怎么修",
            "用codebuddy生成一页网页",
            "workbuddy：整理一份架构图",
            "/wb reset",
            "重置 workbuddy 会话",
        ):
            self.assertTrue(
                is_explicit_workbuddy_request(positive),
                f"Expected explicit WorkBuddy trigger for: {positive!r}",
            )

        for negative in (
            "",
            "帮我写一份周报",
            "今天天气怎么样",
            "myworkbuddy project",
            "workbuddy2 test",
            "/wbbot hello",
            "/wb123 test",
            "刷一下微博 wb 看看热点",
        ):
            self.assertFalse(
                is_explicit_workbuddy_request(negative),
                f"Expected NON-trigger for: {negative!r}",
            )

        for reset_txt in (
            "/workbuddy reset",
            "/wb reset",
            "/codebuddy reset",
            "/workbuddy clear",
            "/wb clear",
            "/workbuddy 重置",
            "/wb 重置",
            "重置workbuddy会话",
            "重置 workbuddy 会话",
            "清空workbuddy会话",
            "清空 workbuddy 会话",
        ):
            self.assertTrue(is_workbuddy_reset_command(reset_txt))
        self.assertFalse(is_workbuddy_reset_command("/wb 帮我重置按钮样式"))

        self.assertEqual(extract_workbuddy_query("/wb"), "")
        self.assertEqual(
            extract_workbuddy_query("/wb 帮我写个HTML汇报卡片"),
            "帮我写个HTML汇报卡片",
        )
        self.assertEqual(
            extract_workbuddy_query("让 workbuddy 帮我做一份竞品对比表"),
            "帮我做一份竞品对比表",
        )
        self.assertEqual(
            extract_workbuddy_query("让 workbuddy 回答：核心结论"),
            "核心结论",
        )

        # Compound detection (WorkBuddy + XiaoMiGuan/XiaoNa OR WorkBuddy + local reminder/browser)
        self.assertFalse(is_compound_workbuddy_request("/wb 帮我生成一份HTML汇报卡片"))
        self.assertTrue(
            is_compound_workbuddy_request("先问小蜜罐明天议程，再让 workbuddy 生成卡片")
        )
        self.assertTrue(
            is_compound_xiaona_request("先问小蜜罐明天议程，再让 workbuddy 生成卡片")
        )
        self.assertTrue(
            is_compound_workbuddy_request("/wb 跑完分析后帮我设个提醒")
        )

        # Verify 0% implicit exposure on ordinary messages
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            admin = "+8613800138000"
            cfg = AgentConfig(
                provider_mode="deepseek",
                deepseek_api_key="sk-valid-wb-ord",
                allowed_senders={admin},
                enable_instant_ack=False,
                state_dir=tmp_path,
            )
            store = StateStore(cfg.sqlite_db_path)
            bridge = CliBridge(quiet=True)
            seen_tools: list[list[str]] = []

            async def must_not_spawn(
                cmd: list[str], cwd: Path, env: dict[str, str], timeout_sec: float
            ) -> tuple[int, str, str]:
                raise AssertionError("WorkBuddy must NEVER be called implicitly!")

            async def ordinary_llm(kwargs: dict[str, Any], call_num: int) -> _MockCompletion:
                seen_tools.append(
                    [
                        t["function"]["name"]
                        for t in kwargs.get("tools", [])
                        if t.get("type") == "function"
                    ]
                )
                return _MockCompletion(
                    choices=[_MockChoice(message=_MockMessage(content="普通对话正常回复。"))]
                )

            orch = HyperTextOrchestrator(
                config=cfg,
                store=store,
                bridge=bridge,
                human_gate=HumanGate(),
                llm_client=_MockAsyncOpenAI(ordinary_llm),
                workbuddy_client=WorkBuddyClient(
                    cfg,
                    runner=must_not_spawn,
                    cli_path_override="/opt/homebrew/bin/codebuddy",
                    allow_repo_dotenv_fallback=False,
                ),
            )

            for ds_mode in (True, False):
                fn_names = [
                    t["function"]["name"]
                    for t in orch._build_tools(is_deepseek_native=ds_mode)
                    if t.get("type") == "function"
                ]
                self.assertNotIn("ask_workbuddy", fn_names)

            ans = await orch.handle_inbound_message(
                InboundMessage(
                    rowid=1,
                    guid="ord-wb-1",
                    sender=admin,
                    text="帮我总结一下今天的重点工作",
                    timestamp_unix=time.time(),
                )
            )
            self.assertEqual(ans, "普通对话正常回复。")
            self.assertEqual(len(seen_tools), 1)
            self.assertNotIn("ask_workbuddy", seen_tools[0])
            store.close()

    async def test_2_non_admin_beta_member_strictly_blocked_zero_spawns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            admin = "+8613800138000"
            member = "+8613900001111"
            cfg = AgentConfig(
                provider_mode="deepseek",
                deepseek_api_key="sk-valid-wb-gate",
                allowed_senders={admin},
                enable_invite_codes=True,
                state_dir=tmp_path,
            )
            store = StateStore(cfg.sqlite_db_path)
            store.allow_user(member, note="beta-member")
            bridge = CliBridge(quiet=True)

            spawns = 0

            async def count_runner(
                cmd: list[str], cwd: Path, env: dict[str, str], timeout_sec: float
            ) -> tuple[int, str, str]:
                nonlocal spawns
                spawns += 1
                return (0, '{"result":"leaked"}', "")

            orch = HyperTextOrchestrator(
                config=cfg,
                store=store,
                bridge=bridge,
                human_gate=HumanGate(),
                workbuddy_client=WorkBuddyClient(
                    cfg,
                    runner=count_runner,
                    cli_path_override="/opt/homebrew/bin/codebuddy",
                    allow_repo_dotenv_fallback=False,
                ),
            )

            expected_deny = "WorkBuddy 连接的是主理人本机工作台，仅限主理人本人调用；其他问题我可以直接帮你处理。"
            for idx, txt in enumerate(
                ("/wb 帮我写代码", "让 workbuddy 生成一份报告", "/wb reset"),
                start=1,
            ):
                denied = await orch.handle_inbound_message(
                    InboundMessage(
                        rowid=idx,
                        guid=f"wb-deny-{idx}",
                        sender=member,
                        text=txt,
                        timestamp_unix=time.time(),
                    )
                )
                self.assertEqual(denied, expected_deny)

            raw_tool = await orch._execute_local_tool(
                member,
                "ask_workbuddy",
                json.dumps({"message": "bypass attempt"}),
            )
            parsed_tool = json.loads(raw_tool)
            self.assertFalse(parsed_tool["ok"])
            self.assertIn("仅限主理人本人调用", parsed_tool["error"])
            self.assertEqual(spawns, 0)
            store.close()

    async def test_3_self_chat_accepts_wb_prefixes_rejects_wbbot_and_anti_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            admin = "+8613800138000"
            chat_db = tmp_path / "chat.db"
            _init_self_chat_db(chat_db, admin)

            cfg = AgentConfig(
                allowed_senders={admin},
                allow_self_chat=True,
                self_chat_prefix="/ai",
                chat_db_path=chat_db,
                state_dir=tmp_path / "state",
            )
            store = StateStore(cfg.sqlite_db_path)
            bridge = MacIMessageBridge(config=cfg, store=store, human_gate=HumanGate())
            bridge.align_cursor_to_latest()
            bridge.record_outbound_echo("收到，我交给 WorkBuddy 处理一下，好了回你。")

            conn = sqlite3.connect(str(chat_db))
            rows = [
                (1, "买咖啡", 1),
                (2, "买咖啡", 0),
                (3, "/wbbot ignore", 1),
                (4, "/wbbot ignore", 0),
                (5, "/wb123 ignore", 1),
                (6, "/wb 帮我生成卡片", 1),
                (7, "/wb 帮我生成卡片", 0),
                (8, "/workbuddy 写个数据分析脚本", 1),
                (9, "/workbuddy 写个数据分析脚本", 0),
                (10, "/codebuddy 检查配置", 1),
                (11, "/codebuddy 检查配置", 0),
                (12, f"{BOT_ECHO_MARKER}/wb 机器人回显", 1),
                (13, "收到，我交给 WorkBuddy 处理一下，好了回你。", 0),
            ]
            for rid, txt, from_me in rows:
                conn.execute(
                    "INSERT INTO message(ROWID, guid, handle_id, text, is_from_me, associated_message_type) "
                    "VALUES (?, ?, 1, ?, ?, 0)",
                    (rid, f"sc-wb-{rid}", txt, from_me),
                )
                conn.execute("INSERT INTO chat_message_join(chat_id, message_id) VALUES (1, ?)", (rid,))
            conn.commit()
            conn.close()

            polled = bridge.poll_new_messages()
            self.assertEqual(
                [m.text for m in polled],
                ["/wb 帮我生成卡片", "/workbuddy 写个数据分析脚本", "/codebuddy 检查配置"],
            )
            for m in polled:
                self.assertTrue(is_explicit_workbuddy_request(m.text))
            store.close()

    def test_4_nvm_cli_discovery_preserves_symlink_bin_dir_and_json_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            nvm_root = tmp_path / "nvm_versions"

            # Create v18.0.0, v24.13.0, and v20.10.0 where `bin/codebuddy` is a symlink to `../lib/.../cli.js`
            for ver in ("v18.0.0", "v24.13.0", "v20.10.0"):
                bin_dir = nvm_root / ver / "bin"
                pkg_dir = nvm_root / ver / "lib" / "node_modules" / "@tencent-ai" / "codebuddy-code"
                bin_dir.mkdir(parents=True)
                pkg_dir.mkdir(parents=True)
                node_bin = bin_dir / "node"
                node_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                node_bin.chmod(node_bin.stat().st_mode | stat.S_IXUSR)

                cli_js = pkg_dir / "cli.js"
                cli_js.write_text("#!/usr/bin/env node\n", encoding="utf-8")
                cli_js.chmod(cli_js.stat().st_mode | stat.S_IXUSR)

                rel_target = os.path.relpath(cli_js, start=bin_dir)
                os.symlink(rel_target, bin_dir / "codebuddy")

            cfg = AgentConfig(state_dir=tmp_path / "state")
            discovered = discover_workbuddy_cli(
                cfg,
                nvm_versions_dir=nvm_root,
                which_fn=lambda _: None,
                static_candidates=(),
                allow_repo_dotenv_fallback=False,
            )
            expected_bin_dir = os.path.abspath(str(nvm_root / "v24.13.0" / "bin"))
            expected_cli = os.path.join(expected_bin_dir, "codebuddy")
            self.assertEqual(discovered, expected_cli)
            self.assertTrue(os.path.islink(discovered))

            child_env = build_workbuddy_child_env(
                discovered, base_env={"PATH": "/usr/bin:/bin"}
            )
            # First PATH segment MUST be `.../v24.13.0/bin` (where `node` lives), NOT `.../lib/node_modules/...`!
            self.assertEqual(child_env["PATH"].split(os.pathsep)[0], expected_bin_dir)

            # JSON output parser coverage: dict `result`, content blocks, JSONL stream, plain text fallback
            err1, txt1 = parse_workbuddy_output('{"type":"result","is_error":false,"result":"任务已完成"}')
            self.assertFalse(err1)
            self.assertEqual(txt1, "任务已完成")

            err2, txt2 = parse_workbuddy_output(
                json.dumps(
                    {
                        "content": [
                            {"type": "thinking", "thinking": "internal"},
                            {"type": "text", "text": "第一段结论"},
                            {"type": "text", "text": "第二段建议"},
                        ]
                    },
                    ensure_ascii=False,
                )
            )
            self.assertFalse(err2)
            self.assertEqual(txt2, "第一段结论\n第二段建议")

            err3, txt3 = parse_workbuddy_output(
                '{"type":"assistant","message":{"content":"中间消息"}}\n'
                '{"type":"result","result":"最终汇总结果"}\n'
            )
            self.assertFalse(err3)
            self.assertEqual(txt3, "最终汇总结果")

            err4, txt4 = parse_workbuddy_output("Plain CLI output fallback")
            self.assertFalse(err4)
            self.assertEqual(txt4, "Plain CLI output fallback")

            # Verify `--doctor` prints masked WorkBuddy status without exposing any paths
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = run_doctor(cfg)
            self.assertEqual(rc, 0)
            doc_out = buf.getvalue()
            self.assertIn("WorkBuddy CLI (本机显式通道): CLI found=", doc_out)
            self.assertIn("timeout=600s", doc_out)

    async def test_5_dual_config_dir_auto_fallback_login_guidance_and_missing_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            alt_wb_dir = tmp_path / "dot_workbuddy"
            alt_wb_dir.mkdir(parents=True)
            cfg = AgentConfig(state_dir=tmp_path / "state")

            # Case A: `~/.codebuddy` returns Authentication required -> auto-retries with `~/.workbuddy` and succeeds!
            env_snapshots: list[dict[str, str]] = []

            async def fallback_runner(
                cmd: list[str], cwd: Path, env: dict[str, str], timeout_sec: float
            ) -> tuple[int, str, str]:
                env_snapshots.append(dict(env))
                if not env.get("CODEBUDDY_CONFIG_DIR"):
                    return (
                        1,
                        '{"is_error":true,"result":"Authentication required. Please run /login"}',
                        "",
                    )
                return (0, '{"type":"result","result":"使用 ~/.workbuddy 配置成功执行！"}', "")

            wb_ok = WorkBuddyClient(
                cfg,
                runner=fallback_runner,
                cli_path_override="/opt/homebrew/bin/codebuddy",
                alt_config_dir=alt_wb_dir,
                allow_repo_dotenv_fallback=False,
            )
            res_ok = await wb_ok.ask("测试双目录回退")
            self.assertTrue(res_ok.ok)
            self.assertEqual(res_ok.formatted_reply, "使用 ~/.workbuddy 配置成功执行！")
            self.assertEqual(len(env_snapshots), 2)
            self.assertNotIn("CODEBUDDY_CONFIG_DIR", env_snapshots[0])
            self.assertEqual(
                Path(env_snapshots[1]["CODEBUDDY_CONFIG_DIR"]).resolve(),
                alt_wb_dir.resolve(),
            )
            self.assertEqual(
                Path(env_snapshots[1]["WORKBUDDY_CONFIG_DIR"]).resolve(),
                alt_wb_dir.resolve(),
            )

            # Case B: Both `~/.codebuddy` and `~/.workbuddy` fail auth -> friendly `/login` guidance!
            async def always_unauth_runner(
                cmd: list[str], cwd: Path, env: dict[str, str], timeout_sec: float
            ) -> tuple[int, str, str]:
                return (1, "", "Error: Authentication failed")

            wb_unauth = WorkBuddyClient(
                cfg,
                runner=always_unauth_runner,
                cli_path_override="/opt/homebrew/bin/codebuddy",
                alt_config_dir=alt_wb_dir,
                allow_repo_dotenv_fallback=False,
            )
            res_unauth = await wb_unauth.ask("测试未登录提示")
            self.assertFalse(res_unauth.ok)
            self.assertEqual(
                res_unauth.formatted_reply,
                "WorkBuddy 命令行已接入就绪，还差一步登录授权：请在 Mac mini 终端运行一次 codebuddy，"
                "输入 /login 完成扫码/账号登录，之后无需重启服务即可直接在手机上调用。",
            )

            # Case C: Missing CLI binary -> 1-line `WORKBUDDY_CLI_PATH` hint
            wb_missing = WorkBuddyClient(
                cfg,
                cli_path_override="",
                allow_repo_dotenv_fallback=False,
            )
            res_missing = await wb_missing.ask("测试未安装提示")
            self.assertFalse(res_missing.ok)
            self.assertIn("WORKBUDDY_CLI_PATH", res_missing.formatted_reply)

    async def test_6_admin_end_to_end_ack_session_reset_lock_and_deliverable_attachments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            admin = "+8613800138000"
            cfg = AgentConfig(
                provider_mode="deepseek",
                deepseek_api_key="sk-valid-wb-e2e",
                allowed_senders={admin},
                enable_instant_ack=True,
                fast_ack_delay_sec=0.03,
                heartbeat_interval_sec=60.0,
                max_heartbeat_per_turn=1,
                state_dir=tmp_path,
            )
            store = StateStore(cfg.sqlite_db_path)
            bridge = CliBridge(quiet=True)

            # Pre-seed an existing untouched file in `workbuddy_workspace` before any run
            ws_dir = cfg.state_dir / "workbuddy_workspace"
            ws_dir.mkdir(parents=True, exist_ok=True)
            untouched_old = ws_dir / "old_archive.html"
            untouched_old.write_text("<html>old</html>", encoding="utf-8")

            in_flight = 0
            max_in_flight = 0
            seen_cmds: list[list[str]] = []

            async def e2e_wb_runner(
                cmd: list[str], cwd: Path, env: dict[str, str], timeout_sec: float
            ) -> tuple[int, str, str]:
                nonlocal in_flight, max_in_flight
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
                seen_cmds.append(list(cmd))
                await asyncio.sleep(0.08)  # Triggers fast ack & tests per-session lock

                prompt_arg = cmd[-1].strip()
                if "竞品对比" in prompt_arg:
                    # Write 2 valid deliverables + 1 sensitive `.env` + 1 oversized `>25MB` file
                    (cwd / "competitor_card.html").write_text(
                        "<!doctype html><h1>Competitors</h1>", encoding="utf-8"
                    )
                    (cwd / "summary_report.pdf").write_bytes(b"%PDF-1.4 mock pdf")
                    (cwd / ".env").write_text("SECRET=1", encoding="utf-8")
                    (cwd / "api_secret_dump.md").write_text("blocked", encoding="utf-8")
                    with (cwd / "huge_video.mp4").open("wb") as fh:
                        fh.seek(25 * 1024 * 1024 + 10)
                        fh.write(b"\x00")

                in_flight -= 1
                return (
                    0,
                    json.dumps(
                        {"type": "result", "result": "**竞品对比**卡片与报告已生成，重点见附件。"},
                        ensure_ascii=False,
                    ),
                    "",
                )

            wb_client = WorkBuddyClient(
                cfg,
                runner=e2e_wb_runner,
                cli_path_override="/opt/homebrew/bin/codebuddy",
                allow_repo_dotenv_fallback=False,
            )

            # 1. Verify per-session `asyncio.Lock` strictly serializes concurrent calls on `ht-wb-owner-1`
            c1, c2 = await asyncio.gather(
                wb_client.ask("并发任务A", sender=admin, store=store),
                wb_client.ask("并发任务B", sender=admin, store=store),
            )
            self.assertTrue(c1.ok and c2.ok)
            self.assertEqual(max_in_flight, 1)

            seen_cmds.clear()
            orch = HyperTextOrchestrator(
                config=cfg,
                store=store,
                bridge=bridge,
                human_gate=HumanGate(),
                workbuddy_client=wb_client,
            )

            # Bare `/wb` prompt
            bare_wb = await orch.handle_inbound_message(
                InboundMessage(rowid=1, guid="wb-bare", sender=admin, text="/wb", timestamp_unix=time.time())
            )
            self.assertEqual(
                bare_wb,
                "请直接附上要交给 WorkBuddy 处理的任务，例如：/wb 帮我生成一份竞品对比 HTML 卡片",
            )
            self.assertEqual(len(seen_cmds), 0)

            # Full `/wb ...` turn -> ack + formatted SMS reply + ONLY newly created valid deliverables attached!
            bridge.outbox.clear()
            bridge.attachments.clear()
            ans = await orch.handle_inbound_message(
                InboundMessage(
                    rowid=2,
                    guid="wb-e2e-1",
                    sender=admin,
                    text="/wb 帮我做一份竞品对比卡片和PDF",
                    timestamp_unix=time.time(),
                )
            )
            self.assertEqual(ans, "竞品对比卡片与报告已生成，重点见附件。")
            self.assertEqual(len(seen_cmds), 1)
            self.assertIn("ht-wb-owner-1", seen_cmds[0])
            self.assertEqual(
                [txt for _, txt in bridge.outbox],
                [
                    "收到，我交给 WorkBuddy 处理一下，好了回你。",
                    "竞品对比卡片与报告已生成，重点见附件。",
                ],
            )
            attached_names = [Path(p).name for _, p, _ in bridge.attachments]
            self.assertEqual(set(attached_names), {"competitor_card.html", "summary_report.pdf"})
            self.assertNotIn("old_archive.html", attached_names)
            self.assertNotIn("huge_video.mp4", attached_names)
            self.assertNotIn("api_secret_dump.md", attached_names)

            # `/wb reset` increments session ID to `ht-wb-owner-2`
            rst_ans = await orch.handle_inbound_message(
                InboundMessage(
                    rowid=3, guid="wb-rst", sender=admin, text="/wb reset", timestamp_unix=time.time()
                )
            )
            self.assertEqual(rst_ans, "🔄 已重置 WorkBuddy 会话上下文，下一条消息将开启全新对话。")

            await orch.handle_inbound_message(
                InboundMessage(
                    rowid=4,
                    guid="wb-e2e-2",
                    sender=admin,
                    text="让 workbuddy 再补充一版摘要",
                    timestamp_unix=time.time(),
                )
            )
            self.assertIn("ht-wb-owner-2", seen_cmds[-1])
            store.close()

    async def test_7_compound_cross_agent_xiaomiguan_and_workbuddy_orchestration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            admin = "+8613800138000"
            cfg = AgentConfig(
                provider_mode="deepseek",
                deepseek_api_key="sk-valid-cross-agent",
                allowed_senders={admin},
                enable_instant_ack=False,
                xiaona_relay_url="https://xiaona-relay.example.com/ask",
                xiaona_relay_key="xn_cross_key",
                state_dir=tmp_path,
            )
            store = StateStore(cfg.sqlite_db_path)
            bridge = CliBridge(quiet=True)

            async def mock_xn_transport(
                url: str, body: bytes, headers: dict[str, str], timeout_sec: float
            ) -> tuple[int, bytes]:
                return (
                    200,
                    json.dumps(
                        {"reply": "明日议程：10:00 发布会彩排，15:00 渠道同步。", "media_urls": []},
                        ensure_ascii=False,
                    ).encode("utf-8"),
                )

            async def mock_wb_runner(
                cmd: list[str], cwd: Path, env: dict[str, str], timeout_sec: float
            ) -> tuple[int, str, str]:
                (cwd / "agenda_card.html").write_text("<!doctype html><h1>Agenda</h1>", encoding="utf-8")
                return (0, '{"type":"result","result":"已生成议程 HTML 卡片 agenda_card.html"}', "")

            exposed_tools: list[list[str]] = []

            async def cross_agent_llm(kwargs: dict[str, Any], call_num: int) -> _MockCompletion:
                exposed_tools.append(
                    [
                        t["function"]["name"]
                        for t in kwargs.get("tools", [])
                        if t.get("type") == "function"
                    ]
                )
                if call_num == 1:
                    return _MockCompletion(
                        choices=[
                            _MockChoice(
                                message=_MockMessage(
                                    content="",
                                    reasoning_content="先向小蜜罐拿议程，再交给 WorkBuddy 生成卡片。",
                                    tool_calls=[
                                        _MockToolCall(
                                            id="tc_xn",
                                            function=_MockFunction(
                                                name="ask_xiaona",
                                                arguments=json.dumps({"message": "查一下明日议程"}, ensure_ascii=False),
                                            ),
                                        ),
                                        _MockToolCall(
                                            id="tc_wb",
                                            function=_MockFunction(
                                                name="ask_workbuddy",
                                                arguments=json.dumps(
                                                    {"message": "把明日议程做成 HTML 卡片"},
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
                                content="已问过小蜜罐并用 WorkBuddy 生成好明日议程卡片，请查收附件。",
                                reasoning_content="",
                            )
                        )
                    ]
                )

            orch = HyperTextOrchestrator(
                config=cfg,
                store=store,
                bridge=bridge,
                human_gate=HumanGate(),
                llm_client=_MockAsyncOpenAI(cross_agent_llm),
                xiaona_client=XiaoNaClient(
                    cfg,
                    transport=mock_xn_transport,
                    enable_keychain_fallback=False,
                    allow_repo_dotenv_fallback=False,
                ),
                workbuddy_client=WorkBuddyClient(
                    cfg,
                    runner=mock_wb_runner,
                    cli_path_override="/opt/homebrew/bin/codebuddy",
                    allow_repo_dotenv_fallback=False,
                ),
            )

            ans = await orch.handle_inbound_message(
                InboundMessage(
                    rowid=1,
                    guid="cross-1",
                    sender=admin,
                    text="先问小蜜罐明天议程，再让 workbuddy 生成卡片",
                    timestamp_unix=time.time(),
                )
            )
            self.assertIn("明日议程卡片", ans or "")
            self.assertIn("ask_xiaona", exposed_tools[0])
            self.assertIn("ask_workbuddy", exposed_tools[0])
            self.assertEqual(len(bridge.attachments), 1)
            self.assertTrue(bridge.attachments[0][1].endswith("agenda_card.html"))
            store.close()


if __name__ == "__main__":
    unittest.main()
