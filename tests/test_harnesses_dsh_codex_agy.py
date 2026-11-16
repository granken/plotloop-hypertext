"""Comprehensive unit tests for HyperText v0.6.0 Multi-Harness Local Agent Matrix (DSH, Codex, AGY)."""

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

from hypertext import (
    AGYClient,
    BOT_ECHO_MARKER,
    AgentConfig,
    CliBridge,
    CodexClient,
    DSHClient,
    HumanGate,
    HyperTextOrchestrator,
    InboundMessage,
    MacIMessageBridge,
    StateStore,
    apply_local_proxy_env,
    detect_explicit_channels,
    extract_agy_query,
    extract_codex_query,
    extract_dsh_query,
    is_agy_reset_command,
    is_codex_reset_command,
    is_dsh_reset_command,
    is_explicit_agy_request,
    is_explicit_codex_request,
    is_explicit_dsh_request,
    parse_agy_output,
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


class TestMultiHarnessDSHCodexAGY(unittest.IsolatedAsyncioTestCase):
    async def test_1_triggers_url_guard_word_boundaries_and_zero_implicit_exposure(self) -> None:
        # DSH triggers & rejections
        for pos_dsh in (
            "/dsh",
            "/dsh 帮我重构解析器",
            "@dsh 检查并发",
            "让 DSH 分析这段架构",
            "用 DeepSeek Harness 写一份设计稿",
            "/dsh reset",
            "重置 dsh 会话",
        ):
            self.assertTrue(is_explicit_dsh_request(pos_dsh), pos_dsh)
        for neg_dsh in (
            "/dshbot test",
            "mydsh project",
            "帮我总结 https://example.com/dsh/docs",
        ):
            self.assertFalse(is_explicit_dsh_request(neg_dsh), neg_dsh)
        self.assertTrue(is_dsh_reset_command("/dsh reset"))
        self.assertTrue(is_dsh_reset_command("清空dsh会话"))
        self.assertEqual(extract_dsh_query("/dsh"), "")
        self.assertEqual(extract_dsh_query("/dsh 分析队列性能"), "分析队列性能")

        # Codex triggers, URL stripping guard & query extraction
        for pos_cdx in (
            "/codex",
            "/cdx",
            "/codex 审查锁安全",
            "/cdx 写单测",
            "@codex check",
            "让 codex 看下 https://github.com/openai/codex",
            "请 cdx 处理一下这个脚本",
            "/codex reset",
            "/cdx clear",
            "重置 codex 会话",
        ):
            self.assertTrue(is_explicit_codex_request(pos_cdx), pos_cdx)
        for neg_cdx in (
            "/codexbot hi",
            "/cdx123 hi",
            "mycodex tool",
            "帮我总结这篇文章 https://github.com/openai/codex/releases",
        ):
            self.assertFalse(is_explicit_codex_request(neg_cdx), neg_cdx)
        self.assertTrue(is_codex_reset_command("/cdx reset"))
        self.assertEqual(extract_codex_query("/cdx"), "")
        self.assertEqual(
            extract_codex_query("让 codex 看下 https://github.com/openai/codex"),
            "看下 https://github.com/openai/codex",
        )

        # AGY triggers & word-boundary rejection (`strategy`, `stagy`, `/agybot`)
        for pos_agy in (
            "/agy",
            "/antigravity",
            "/agy 设计前端状态机",
            "@agy review",
            "让 AGY 给出架构建议",
            "问下 antigravity 这个交互怎么做",
            "/agy reset",
            "重置 agy 会话",
        ):
            self.assertTrue(is_explicit_agy_request(pos_agy), pos_agy)
        for neg_agy in (
            "/agybot hi",
            "product strategy document",
            "stagy layout",
            "总结链接 https://example.com/agy/overview",
        ):
            self.assertFalse(is_explicit_agy_request(neg_agy), neg_agy)
        self.assertTrue(is_agy_reset_command("/antigravity reset"))
        self.assertEqual(extract_agy_query("/agy"), "")
        self.assertEqual(extract_agy_query("/agy -评估状态机"), "评估状态机")
        self.assertEqual(extract_agy_query("让 agy 回答：-评估状态机"), "-评估状态机")

        # Ordered multi-channel detection
        self.assertEqual(
            detect_explicit_channels("让 AGY、DSH 和 Codex 分别对比一下"),
            ["agy", "dsh", "codex"],
        )

        # Ordinary messages expose 0% of ask_dsh / ask_codex / ask_agy
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            admin = "+8613800138000"
            cfg = AgentConfig(
                provider_mode="deepseek",
                deepseek_api_key="sk-valid-ord-matrix",
                allowed_senders={admin},
                enable_instant_ack=False,
                state_dir=tmp_path,
            )
            store = StateStore(cfg.sqlite_db_path)
            bridge = CliBridge(quiet=True)
            seen_fns: list[list[str]] = []

            async def ord_llm(kwargs: dict[str, Any], call_num: int) -> _MockCompletion:
                seen_fns.append(
                    [
                        t["function"]["name"]
                        for t in kwargs.get("tools", [])
                        if t.get("type") == "function"
                    ]
                )
                return _MockCompletion(
                    choices=[_MockChoice(message=_MockMessage(content="普通链接总结完成。"))]
                )

            orch = HyperTextOrchestrator(
                config=cfg,
                store=store,
                bridge=bridge,
                human_gate=HumanGate(),
                llm_client=_MockAsyncOpenAI(ord_llm),
            )
            ans = await orch.handle_inbound_message(
                InboundMessage(
                    rowid=1,
                    guid="ord-url",
                    sender=admin,
                    text="帮我读一下 https://github.com/openai/codex 的介绍",
                    timestamp_unix=time.time(),
                )
            )
            self.assertEqual(ans, "普通链接总结完成。")
            self.assertEqual(len(seen_fns), 1)
            for forbidden_tool in ("ask_xiaona", "ask_workbuddy", "ask_dsh", "ask_codex", "ask_agy"):
                self.assertNotIn(forbidden_tool, seen_fns[0])
            store.close()

    async def test_2_non_admin_beta_member_strictly_blocked_across_all_harnesses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            admin = "+8613800138000"
            member = "+8613900001111"
            cfg = AgentConfig(
                provider_mode="deepseek",
                deepseek_api_key="sk-valid-gate-matrix",
                allowed_senders={admin},
                enable_invite_codes=True,
                state_dir=tmp_path,
            )
            store = StateStore(cfg.sqlite_db_path)
            store.allow_user(member, note="beta")
            bridge = CliBridge(quiet=True)
            spawns = 0

            async def count_runner(
                cmd: list[str], cwd: Path, env: dict[str, str], timeout_sec: float
            ) -> tuple[int, str, str]:
                nonlocal spawns
                spawns += 1
                return (0, "leaked", "")

            orch = HyperTextOrchestrator(
                config=cfg,
                store=store,
                bridge=bridge,
                human_gate=HumanGate(),
                dsh_client=DSHClient(
                    cfg, runner=count_runner, cli_path_override="/opt/homebrew/bin/dsh", allow_repo_dotenv_fallback=False
                ),
                codex_client=CodexClient(
                    cfg, runner=count_runner, cli_path_override="/opt/homebrew/bin/codex", allow_repo_dotenv_fallback=False
                ),
                agy_client=AGYClient(
                    cfg, runner=count_runner, cli_path_override="/opt/homebrew/bin/agy", allow_repo_dotenv_fallback=False
                ),
            )

            for idx, (attempt, label) in enumerate(
                (
                    ("/dsh 分析代码", "DSH"),
                    ("/codex 审查脚本", "Codex"),
                    ("/cdx reset", "Codex"),
                    ("/agy 设计页面", "AGY"),
                    ("让 DSH 和 Codex 对比一下", "DSH"),
                ),
                start=1,
            ):
                denied = await orch.handle_inbound_message(
                    InboundMessage(
                        rowid=idx,
                        guid=f"deny-h-{idx}",
                        sender=member,
                        text=attempt,
                        timestamp_unix=time.time(),
                    )
                )
                self.assertEqual(
                    denied,
                    f"{label} 连接的是主理人本机工作台，仅限主理人本人调用；其他问题我可以直接帮你处理。",
                )

            for tool_name in ("ask_dsh", "ask_codex", "ask_agy"):
                raw_res = await orch._execute_local_tool(
                    member, tool_name, json.dumps({"message": "bypass"})
                )
                parsed = json.loads(raw_res)
                self.assertFalse(parsed["ok"])
                self.assertIn("仅限主理人本人调用", parsed["error"])

            self.assertEqual(spawns, 0)
            store.close()

    async def test_3_self_chat_accepts_dsh_codex_cdx_agy_and_preserves_antiloop(self) -> None:
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
            bridge.record_outbound_echo("收到，我交给 Codex 处理一下，好了回你。")

            conn = sqlite3.connect(str(chat_db))
            rows = [
                (1, "今晚买水果", 1),
                (2, "/dshbot ignore", 1),
                (3, "/codexbot ignore", 1),
                (4, "/agybot ignore", 1),
                (5, "/dsh 分析架构", 1),
                (6, "/dsh 分析架构", 0),
                (7, "/codex 审查锁", 1),
                (8, "/codex 审查锁", 0),
                (9, "/cdx 跑测试", 1),
                (10, "/agy 写前端方案", 1),
                (11, "/antigravity 检查状态机", 1),
                (12, f"{BOT_ECHO_MARKER}/codex 机器人回显", 1),
                (13, "收到，我交给 Codex 处理一下，好了回你。", 0),
            ]
            for rid, txt, from_me in rows:
                conn.execute(
                    "INSERT INTO message(ROWID, guid, handle_id, text, is_from_me, associated_message_type) "
                    "VALUES (?, ?, 1, ?, ?, 0)",
                    (rid, f"sc-h-{rid}", txt, from_me),
                )
                conn.execute("INSERT INTO chat_message_join(chat_id, message_id) VALUES (1, ?)", (rid,))
            conn.commit()
            conn.close()

            polled = bridge.poll_new_messages()
            self.assertEqual(
                [m.text for m in polled],
                [
                    "/dsh 分析架构",
                    "/codex 审查锁",
                    "/cdx 跑测试",
                    "/agy 写前端方案",
                    "/antigravity 检查状态机",
                ],
            )
            store.close()

    def test_4_local_proxy_autodetect_no_proxy_merge_and_doctor_redaction(self) -> None:
        cfg = AgentConfig()

        # Probe=True -> sets 127.0.0.1:7897 AND merges NO_PROXY with 127.0.0.1,localhost,::1
        env1 = {"NO_PROXY": "internal.corp"}
        apply_local_proxy_env(
            env1, cfg, proxy_probe=lambda h, p: (h == "127.0.0.1" and p == 7897), allow_repo_dotenv_fallback=False
        )
        self.assertEqual(env1["HTTPS_PROXY"], "http://127.0.0.1:7897")
        self.assertEqual(env1["http_proxy"], "http://127.0.0.1:7897")
        for host in ("internal.corp", "127.0.0.1", "localhost", "::1"):
            self.assertIn(host, env1["NO_PROXY"].split(","))
            self.assertIn(host, env1["no_proxy"].split(","))

        # Existing HTTPS_PROXY is respected, while NO_PROXY still gets localhost entries
        env2 = {"HTTPS_PROXY": "http://127.0.0.1:9000"}
        apply_local_proxy_env(env2, cfg, proxy_probe=lambda h, p: True, allow_repo_dotenv_fallback=False)
        self.assertEqual(env2["HTTPS_PROXY"], "http://127.0.0.1:9000")
        self.assertIn("127.0.0.1", env2["NO_PROXY"])

        # Doctor output shows masked readiness for DSH, Codex, AGY without personal paths
        with tempfile.TemporaryDirectory() as tmp:
            doc_cfg = AgentConfig(state_dir=Path(tmp) / "state")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = run_doctor(doc_cfg)
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("DSH CLI (本机显式通道): CLI found=", out)
            self.assertIn("Codex CLI (本机显式通道): CLI found=", out)
            self.assertIn("AGY CLI (本机显式通道): CLI found=", out)

    async def test_5_dsh_client_magpie_patch_session_resume_stale_retry_and_deliverables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            admin = "+8613800138000"
            dsh_home = tmp_path / "dot_dsh"
            dsh_home.mkdir(parents=True)
            (dsh_home / "settings.yaml").write_text("providers:\n  magpie:\n    baseUrl: http://127.0.0.1:3425\n", encoding="utf-8")

            cfg = AgentConfig(
                provider_mode="deepseek",
                deepseek_api_key="sk-valid-dsh-fallback",
                allowed_senders={admin},
                enable_instant_ack=True,
                fast_ack_delay_sec=0.02,
                state_dir=tmp_path / "state",
            )
            store = StateStore(cfg.sqlite_db_path)
            bridge = CliBridge(quiet=True)

            recorded_calls: list[dict[str, Any]] = []

            async def dsh_runner(
                cmd: list[str], cwd: Path, env: dict[str, str], timeout_sec: float
            ) -> tuple[int, str, str]:
                await asyncio.sleep(0.05)
                recorded_calls.append({"cmd": list(cmd), "env": dict(env), "cwd": cwd})
                # Stale session test: if `--session-id stale-sid` is passed, fail once!
                if "--session-id" in cmd and cmd[cmd.index("--session-id") + 1] == "stale-sid":
                    return (1, '{"type":"error","message":"Session not found: stale-sid"}\n', "")
                (cwd / "arch_diagram.html").write_text("<!doctype html><h1>DSH</h1>", encoding="utf-8")
                return (
                    0,
                    '{"type":"thinking","text":"..."}\n'
                    '{"type":"final","text":"### 架构评审\\n- **建议一**：拆分读写通道","sessionId":"dsh-live-01"}\n',
                    "",
                )

            dsh_client = DSHClient(
                cfg,
                runner=dsh_runner,
                cli_path_override="/opt/homebrew/bin/dsh",
                dsh_home_dir=dsh_home,
                magpie_probe=lambda h, p: (h == "127.0.0.1" and p == 3425),
                allow_repo_dotenv_fallback=False,
            )
            orch = HyperTextOrchestrator(
                config=cfg,
                store=store,
                bridge=bridge,
                human_gate=HumanGate(),
                dsh_client=dsh_client,
            )

            # Turn 1: Magpie `--patch` injected, `.ht_magpie_model.patch.yml` NOT sent as attachment,
            # `arch_diagram.html` IS sent as attachment, `sessionId` persisted!
            ans1 = await orch.handle_inbound_message(
                InboundMessage(rowid=1, guid="dsh-1", sender=admin, text="/dsh 评审读写架构", timestamp_unix=time.time())
            )
            self.assertIn("架构评审", ans1 or "")
            self.assertNotIn("###", ans1 or "")
            self.assertEqual(bridge.outbox[0][1], "收到，我交给 DSH 处理一下，好了回你。")
            self.assertEqual(len(recorded_calls), 1)
            cmd1 = recorded_calls[0]["cmd"]
            self.assertIn("--patch", cmd1)
            patch_file = Path(cmd1[cmd1.index("--patch") + 1])
            self.assertEqual(patch_file.name, ".ht_magpie_model.patch.yml")
            self.assertIn("provider: magpie", patch_file.read_text(encoding="utf-8"))
            self.assertEqual(recorded_calls[0]["env"]["DSH_PERMISSION_MODE"], "danger-full-access")
            self.assertIn("127.0.0.1", recorded_calls[0]["env"]["NO_PROXY"])

            attached = [Path(p).name for _, p, _ in bridge.attachments]
            self.assertEqual(attached, ["arch_diagram.html"])
            self.assertEqual(dsh_client.get_session_id(store, admin), "dsh-live-01")

            # Turn 2: Resumes with `--session-id dsh-live-01`; also test stale session auto-recovery!
            store.set_meta(dsh_client._meta_key(admin), "stale-sid")
            recorded_calls.clear()
            ans2 = await orch.handle_inbound_message(
                InboundMessage(rowid=2, guid="dsh-2", sender=admin, text="/dsh 继续补充细节", timestamp_unix=time.time())
            )
            self.assertIn("拆分读写通道", ans2 or "")
            self.assertEqual(len(recorded_calls), 2)
            self.assertIn("stale-sid", recorded_calls[0]["cmd"])
            self.assertNotIn("--session-id", recorded_calls[1]["cmd"])

            # `/dsh reset` clears stored sessionId
            rst = await orch.handle_inbound_message(
                InboundMessage(rowid=3, guid="dsh-3", sender=admin, text="/dsh reset", timestamp_unix=time.time())
            )
            self.assertEqual(rst, "🔄 已重置 DSH 会话上下文，下一条消息将开启全新对话。")
            self.assertEqual(dsh_client.get_session_id(store, admin), "")
            store.close()

    async def test_6_codex_client_exec_resume_flags_model_fallback_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            admin = "+8613800138000"
            cfg = AgentConfig(
                provider_mode="deepseek",
                deepseek_api_key="sk-valid-codex-test",
                allowed_senders={admin},
                enable_instant_ack=False,
                state_dir=tmp_path / "state",
            )
            store = StateStore(cfg.sqlite_db_path)
            bridge = CliBridge(quiet=True)

            seen_cmds: list[list[str]] = []

            async def codex_runner(
                cmd: list[str], cwd: Path, env: dict[str, str], timeout_sec: float
            ) -> tuple[int, str, str]:
                seen_cmds.append(list(cmd))
                model_arg = cmd[cmd.index("-m") + 1]
                # Simulate `gpt-6-luna` unsupported -> auto-falls back to `gpt-5.6-terra`!
                if model_arg == "gpt-6-luna":
                    return (
                        1,
                        '{"type":"error","message":"The \'gpt-6-luna\' model is not supported when using Codex with a ChatGPT account."}\n',
                        "",
                    )
                if "resume" in cmd and "stale-thread-99" in cmd:
                    return (1, "", "Error: rollout not found for thread stale-thread-99")

                out_path = Path(cmd[cmd.index("-o") + 1])
                out_path.write_text("**审计结论**：已修复死锁隐患。", encoding="utf-8")
                if "resume" not in cmd:
                    (cwd / "audit_patch.md").write_text("# Patch Notes\n", encoding="utf-8")
                return (
                    0,
                    '{"type":"thread.started","thread_id":"019d-codex-thread-1"}\n'
                    '{"type":"turn.completed"}\n',
                    "",
                )

            codex_client = CodexClient(
                cfg,
                runner=codex_runner,
                cli_path_override="/opt/homebrew/bin/codex",
                proxy_probe=lambda h, p: True,
                allow_repo_dotenv_fallback=False,
            )
            orch = HyperTextOrchestrator(
                config=cfg,
                store=store,
                bridge=bridge,
                human_gate=HumanGate(),
                codex_client=codex_client,
            )

            # First turn: tries `gpt-6-luna` -> auto-retries `gpt-5.6-terra`, verifies exact flags & cleans `.ht_codex_out_*`
            ans1 = await orch.handle_inbound_message(
                InboundMessage(rowid=1, guid="cdx-1", sender=admin, text="/codex 审计并发锁", timestamp_unix=time.time())
            )
            self.assertEqual(ans1, "审计结论：已修复死锁隐患。")
            self.assertEqual(len(seen_cmds), 2)
            self.assertEqual(seen_cmds[0][seen_cmds[0].index("-m") + 1], "gpt-6-luna")
            self.assertEqual(seen_cmds[1][seen_cmds[1].index("-m") + 1], "gpt-5.6-terra")
            for required_tok in (
                "exec",
                "notify=[]",
                "mcp_servers={}",
                'model_reasoning_effort="medium"',
                "--dangerously-bypass-approvals-and-sandbox",
                "--skip-git-repo-check",
                "--json",
                "-C",
                "-o",
            ):
                self.assertIn(required_tok, seen_cmds[1])
            self.assertEqual(codex_client.get_thread_id(store, admin), "019d-codex-thread-1")
            leftover_out = list((cfg.state_dir / "codex_workspace").glob(".ht_codex_out_*"))
            self.assertEqual(leftover_out, [])
            self.assertEqual([Path(p).name for _, p, _ in bridge.attachments], ["audit_patch.md"])

            # Second turn: uses `codex exec resume 019d-codex-thread-1 ...` (without `-C`)!
            seen_cmds.clear()
            cfg.codex_model = "gpt-5.6-terra"
            ans2 = await orch.handle_inbound_message(
                InboundMessage(rowid=2, guid="cdx-2", sender=admin, text="/cdx 再检查一下边界条件", timestamp_unix=time.time())
            )
            self.assertEqual(ans2, "审计结论：已修复死锁隐患。")
            self.assertEqual(len(seen_cmds), 1)
            self.assertEqual(seen_cmds[0][1:4], ["exec", "resume", "019d-codex-thread-1"])
            self.assertNotIn("-C", seen_cmds[0])

            # Stale thread_id auto-fallback to fresh `exec`
            store.set_meta(codex_client._meta_key(admin), "stale-thread-99")
            seen_cmds.clear()
            await orch.handle_inbound_message(
                InboundMessage(rowid=3, guid="cdx-3", sender=admin, text="/codex 重新检查", timestamp_unix=time.time())
            )
            self.assertEqual(len(seen_cmds), 2)
            self.assertEqual(seen_cmds[0][1:4], ["exec", "resume", "stale-thread-99"])
            self.assertNotIn("resume", seen_cmds[1])

            # `/codex reset` clears stored thread_id
            rst = await orch.handle_inbound_message(
                InboundMessage(rowid=4, guid="cdx-4", sender=admin, text="/codex reset", timestamp_unix=time.time())
            )
            self.assertEqual(rst, "🔄 已重置 Codex 会话上下文，下一条消息将开启全新对话。")
            self.assertEqual(codex_client.get_thread_id(store, admin), "")
            store.close()

    async def test_7_agy_client_strict_trailing_p_flag_conversation_resume_and_parsers(self) -> None:
        # Flexible parser checks: JSON dict, JSONL, plain text
        self.assertEqual(
            parse_agy_output('{"response":"OK 1","conversation_id":"conv-1"}'),
            (False, "OK 1", "conv-1"),
        )
        self.assertEqual(
            parse_agy_output('{"type":"event","conversationId":"conv-2"}\n{"result":"OK 2"}\n'),
            (False, "OK 2", "conv-2"),
        )
        self.assertEqual(
            parse_agy_output("Plain AGY text reply"),
            (False, "Plain AGY text reply", ""),
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            admin = "+8613800138000"
            cfg = AgentConfig(
                provider_mode="deepseek",
                deepseek_api_key="sk-valid-agy-test",
                allowed_senders={admin},
                enable_instant_ack=False,
                state_dir=tmp_path / "state",
            )
            store = StateStore(cfg.sqlite_db_path)
            bridge = CliBridge(quiet=True)

            captured: list[tuple[list[str], dict[str, str]]] = []

            async def agy_runner(
                cmd: list[str], cwd: Path, env: dict[str, str], timeout_sec: float
            ) -> tuple[int, str, str]:
                captured.append((list(cmd), dict(env)))
                if "--conversation" in cmd and cmd[cmd.index("--conversation") + 1] == "stale-conv":
                    return (1, "", "Error: conversation not found")
                return (
                    0,
                    json.dumps(
                        {"response": "**AGY建议**：采用声明式状态表。", "conversation_id": "agy-conv-88"},
                        ensure_ascii=False,
                    ),
                    "",
                )

            agy_client = AGYClient(
                cfg,
                runner=agy_runner,
                cli_path_override="/opt/homebrew/bin/agy",
                proxy_probe=lambda h, p: True,
                allow_repo_dotenv_fallback=False,
            )
            orch = HyperTextOrchestrator(
                config=cfg,
                store=store,
                bridge=bridge,
                human_gate=HumanGate(),
                agy_client=agy_client,
            )

            # Prompt starting with `-` MUST STILL keep `["-p", prompt]` at the VERY END of argv!
            ans1 = await orch.handle_inbound_message(
                InboundMessage(
                    rowid=1,
                    guid="agy-1",
                    sender=admin,
                    text="让 agy 回答：--对比声明式与命令式状态机",
                    timestamp_unix=time.time(),
                )
            )
            self.assertEqual(ans1, "AGY建议：采用声明式状态表。")
            cmd1, env1 = captured[0]
            self.assertEqual(cmd1[-2:], ["-p", "--对比声明式与命令式状态机"])
            self.assertIn("--dangerously-skip-permissions", cmd1)
            self.assertIn("--disable-slash-commands", cmd1)
            self.assertEqual(cmd1[cmd1.index("--model") + 1], "gemini-3.8-flash-high")
            self.assertIn("127.0.0.1", env1["NO_PROXY"])
            self.assertIn("localhost", env1["no_proxy"])
            self.assertEqual(agy_client.get_conversation_id(store, admin), "agy-conv-88")

            # Turn 2 resumes with `--conversation agy-conv-88` BEFORE `--output-format json -p <prompt>`
            captured.clear()
            await orch.handle_inbound_message(
                InboundMessage(rowid=2, guid="agy-2", sender=admin, text="/antigravity 展开讲讲", timestamp_unix=time.time())
            )
            cmd2, _ = captured[0]
            self.assertIn("--conversation", cmd2)
            self.assertEqual(cmd2[cmd2.index("--conversation") + 1], "agy-conv-88")
            self.assertEqual(cmd2[-2:], ["-p", "展开讲讲"])

            # Stale conversation auto-retry + `/agy reset`
            store.set_meta(agy_client._meta_key(admin), "stale-conv")
            captured.clear()
            await orch.handle_inbound_message(
                InboundMessage(rowid=3, guid="agy-3", sender=admin, text="/agy 继续", timestamp_unix=time.time())
            )
            self.assertEqual(len(captured), 2)
            self.assertIn("stale-conv", captured[0][0])
            self.assertNotIn("--conversation", captured[1][0])

            rst = await orch.handle_inbound_message(
                InboundMessage(rowid=4, guid="agy-4", sender=admin, text="/agy reset", timestamp_unix=time.time())
            )
            self.assertEqual(rst, "🔄 已重置 AGY 会话上下文，下一条消息将开启全新对话。")
            self.assertEqual(agy_client.get_conversation_id(store, admin), "")
            store.close()

    async def test_8_multi_harness_arena_compound_turn_dsh_codex_and_agy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            admin = "+8613800138000"
            cfg = AgentConfig(
                provider_mode="deepseek",
                deepseek_api_key="sk-valid-arena-key",
                allowed_senders={admin},
                enable_instant_ack=True,
                fast_ack_delay_sec=0.02,
                state_dir=tmp_path / "state",
            )
            store = StateStore(cfg.sqlite_db_path)
            bridge = CliBridge(quiet=True)

            async def arena_dsh(cmd: list[str], cwd: Path, env: dict[str, str], t: float) -> tuple[int, str, str]:
                await asyncio.sleep(0.03)
                return (0, '{"type":"final","text":"DSH主张事件溯源","sessionId":"s1"}\n', "")

            async def arena_codex(cmd: list[str], cwd: Path, env: dict[str, str], t: float) -> tuple[int, str, str]:
                await asyncio.sleep(0.03)
                Path(cmd[cmd.index("-o") + 1]).write_text("Codex主张SQLite WAL单写者", encoding="utf-8")
                return (0, '{"type":"thread.started","thread_id":"t1"}\n', "")

            async def arena_agy(cmd: list[str], cwd: Path, env: dict[str, str], t: float) -> tuple[int, str, str]:
                await asyncio.sleep(0.03)
                return (0, '{"response":"AGY主张状态机显式转换","conversation_id":"c1"}', "")

            seen_tools: list[list[str]] = []
            tool_outputs_in_round2: list[str] = []

            async def arena_llm(kwargs: dict[str, Any], call_num: int) -> _MockCompletion:
                seen_tools.append(
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
                                    reasoning_content="依次调用 DSH、Codex、AGY 收集三方方案。",
                                    tool_calls=[
                                        _MockToolCall(
                                            id="tc_dsh",
                                            function=_MockFunction(
                                                name="ask_dsh",
                                                arguments=json.dumps({"message": "给出队列持久化方案"}, ensure_ascii=False),
                                            ),
                                        ),
                                        _MockToolCall(
                                            id="tc_cdx",
                                            function=_MockFunction(
                                                name="ask_codex",
                                                arguments=json.dumps({"message": "给出队列持久化方案"}, ensure_ascii=False),
                                            ),
                                        ),
                                        _MockToolCall(
                                            id="tc_agy",
                                            function=_MockFunction(
                                                name="ask_agy",
                                                arguments=json.dumps({"message": "给出队列持久化方案"}, ensure_ascii=False),
                                            ),
                                        ),
                                    ],
                                )
                            )
                        ]
                    )
                for m in kwargs.get("messages", []):
                    if m.get("role") == "tool":
                        tool_outputs_in_round2.append(str(m.get("content", "")))
                return _MockCompletion(
                    choices=[
                        _MockChoice(
                            message=_MockMessage(
                                content=(
                                    "三方方案对比完毕：DSH 侧重事件溯源，Codex 推荐 SQLite WAL 单写者，"
                                    "AGY 强调显式状态机；本地优先场景建议采用 WAL + 状态机组合。"
                                ),
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
                llm_client=_MockAsyncOpenAI(arena_llm),
                dsh_client=DSHClient(
                    cfg, runner=arena_dsh, cli_path_override="/opt/homebrew/bin/dsh", magpie_probe=lambda h, p: False, allow_repo_dotenv_fallback=False
                ),
                codex_client=CodexClient(
                    cfg, runner=arena_codex, cli_path_override="/opt/homebrew/bin/codex", proxy_probe=lambda h, p: False, allow_repo_dotenv_fallback=False
                ),
                agy_client=AGYClient(
                    cfg, runner=arena_agy, cli_path_override="/opt/homebrew/bin/agy", proxy_probe=lambda h, p: False, allow_repo_dotenv_fallback=False
                ),
            )

            ans = await orch.handle_inbound_message(
                InboundMessage(
                    rowid=1,
                    guid="arena-1",
                    sender=admin,
                    text="让 DSH、Codex 和 AGY 分别给出方案对比",
                    timestamp_unix=time.time(),
                )
            )
            self.assertIn("三方方案对比完毕", ans or "")
            self.assertEqual(
                bridge.outbox[0][1],
                "收到，我调起 DSH、Codex、AGY 处理一下，好了回你。",
            )
            for expected_fn in ("ask_dsh", "ask_codex", "ask_agy"):
                self.assertIn(expected_fn, seen_tools[0])
            self.assertNotIn("ask_xiaona", seen_tools[0])
            self.assertNotIn("ask_workbuddy", seen_tools[0])
            joined_tools = "\n".join(tool_outputs_in_round2)
            self.assertIn("DSH主张事件溯源", joined_tools)
            self.assertIn("Codex主张SQLite WAL单写者", joined_tools)
            self.assertIn("AGY主张状态机显式转换", joined_tools)
            store.close()


if __name__ == "__main__":
    unittest.main()
