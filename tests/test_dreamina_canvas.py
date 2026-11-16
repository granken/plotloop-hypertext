"""Comprehensive tests for HyperText v0.6.2 Explicit-Only Dreamina Canvas (`即梦画布`) Integration."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hypertext import (
    AgentConfig,
    CliBridge,
    CodexClient,
    DreaminaClient,
    HumanGate,
    HyperTextOrchestrator,
    InboundMessage,
    MacIMessageBridge,
    StateStore,
    build_dreamina_child_env,
    detect_explicit_channels,
    discover_dreamina_cli,
    extract_dreamina_query,
    is_dreamina_reset_command,
    is_explicit_dreamina_request,
    normalize_sender,
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
        self.calls = 0

    async def create(self, **kwargs: Any) -> _MockCompletion:
        self.calls += 1
        return await self._handler(kwargs, self.calls)


class _MockChatNamespace:
    def __init__(self, handler: Any) -> None:
        self.completions = _MockCompletionsEndpoint(handler)


class _MockAsyncOpenAI:
    def __init__(self, handler: Any) -> None:
        self.chat = _MockChatNamespace(handler)


def _init_mock_chat_db(db_path: Path) -> None:
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
        CREATE TABLE attachment (
            ROWID INTEGER PRIMARY KEY,
            filename TEXT,
            mime_type TEXT,
            transfer_name TEXT,
            total_bytes INTEGER DEFAULT 0
        );
        CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
        """
    )
    conn.commit()
    conn.close()


class TestDreaminaCanvasV062(unittest.IsolatedAsyncioTestCase):
    def test_explicit_triggers_url_stripping_word_boundaries_and_channel_order(self) -> None:
        for pos in (
            "/即梦 一只戴宇航头盔的橘猫",
            "/即梦画布 赛博朋克雨夜海报",
            "／即梦 电影感海边日落",
            "/jimeng futuristic city",
            "/dreamina neon alley",
            "/dreamina-canvas Minimalist poster",
            "/jm 极简科技风封面",
            "@即梦 帮我画一张海报",
            "让即梦画一张春日樱花壁纸",
            "请用 dreamina 生成一段 5 秒镜头视频",
            "找 jm 生成一张科技感封面",
            "/即梦 reset",
            "/jimeng clear",
            "重置即梦画布",
        ):
            self.assertTrue(is_explicit_dreamina_request(pos), f"Expected True for: {pos!r}")

        for neg in (
            "",
            "帮我画一张海报",
            "生成一段纯音乐伴奏",
            "做一段 5 秒的产品宣传视频",
            "今天北京天气怎么样",
            "帮我总结这个链接 https://jimeng.jianying.com/ai-tool/canvas/abc123",
            "看看这个页面 https://dreamina.capcut.com/s/xyz987",
            "/jmbot 你好",
            "/jm123 测试",
            "/dreaminabot hello",
            "mydreamina tool",
            "jimeng2 notes",
            "请检查 repo/jm/index.ts 文件",
        ):
            self.assertFalse(is_explicit_dreamina_request(neg), f"Expected False for: {neg!r}")

        self.assertTrue(is_dreamina_reset_command("/即梦 reset"))
        self.assertTrue(is_dreamina_reset_command("/jm 重置画布"))
        self.assertTrue(is_dreamina_reset_command("清空即梦画布！"))
        self.assertFalse(is_dreamina_reset_command("/即梦 画一个 reset 按钮图标"))

        self.assertEqual(
            extract_dreamina_query("/即梦 一只橘猫在月球喝咖啡 3:4"),
            "一只橘猫在月球喝咖啡 3:4",
        )
        self.assertEqual(
            extract_dreamina_query("让即梦帮我画一张：极简瑞士风格海报"),
            "极简瑞士风格海报",
        )
        self.assertEqual(extract_dreamina_query("/jm"), "")

        self.assertEqual(
            detect_explicit_channels("先让 Codex 写文案，再用即梦生成一张配套海报"),
            ["codex", "dreamina"],
        )
        self.assertEqual(
            detect_explicit_channels("先用即梦画海报，再交给 WorkBuddy 嵌入网页"),
            ["dreamina", "workbuddy"],
        )

    def test_domestic_proxy_stripping_cli_discovery_doctor_and_zero_implicit_exposure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "bin" / "dreamina-canvas"
            fake_bin.parent.mkdir(parents=True, exist_ok=True)
            fake_bin.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
            fake_bin.chmod(0o755)

            env_file = tmp_path / ".env"
            env_file.write_text(
                f"DEEPSEEK_API_KEY=sk-valid-dm-key\nDREAMINA_CLI_PATH={fake_bin}\n",
                encoding="utf-8",
            )
            cfg = AgentConfig.from_env(env_file=env_file, environ={})
            found = discover_dreamina_cli(cfg, which_fn=lambda _: None, static_candidates=())
            self.assertEqual(found, os.path.abspath(str(fake_bin)))

            base_env = {
                "PATH": "/usr/bin:/bin",
                "http_proxy": "http://127.0.0.1:7897",
                "https_proxy": "http://127.0.0.1:7897",
                "HTTP_PROXY": "http://127.0.0.1:7897",
                "HTTPS_PROXY": "http://127.0.0.1:7897",
                "ALL_PROXY": "socks5://127.0.0.1:7897",
            }
            clean_env = build_dreamina_child_env(
                str(fake_bin), cfg, base_env=base_env, allow_repo_dotenv_fallback=False
            )
            for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
                self.assertNotIn(k, clean_env)
            self.assertTrue(clean_env["PATH"].startswith(str(fake_bin.parent)))

            # Explicit DREAMINA_HTTP_PROXY opt-in
            env_file.write_text(
                f"DEEPSEEK_API_KEY=sk-valid-dm-key\n"
                f"DREAMINA_CLI_PATH={fake_bin}\n"
                "DREAMINA_HTTP_PROXY=http://127.0.0.1:8899\n",
                encoding="utf-8",
            )
            opt_in_env = build_dreamina_child_env(
                str(fake_bin), cfg, base_env=base_env, allow_repo_dotenv_fallback=False
            )
            self.assertEqual(opt_in_env["HTTP_PROXY"], "http://127.0.0.1:8899")
            self.assertEqual(opt_in_env["HTTPS_PROXY"], "http://127.0.0.1:8899")

            # Doctor output check & redaction
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = run_doctor(cfg, env_file=env_file)
            self.assertEqual(rc, 0)
            doc_txt = buf.getvalue()
            self.assertIn("Dreamina Canvas CLI (即梦显式通道): CLI found=True", doc_txt)
            self.assertNotIn("sk-valid-dm-key", doc_txt)

            # 0% implicit tool exposure on ordinary turns
            store = StateStore(tmp_path / "state.sqlite3")
            orch = HyperTextOrchestrator(
                config=cfg,
                store=store,
                bridge=CliBridge(quiet=True),
                human_gate=HumanGate(),
            )
            default_tools = [
                t.get("function", {}).get("name")
                for t in orch._build_tools(is_deepseek_native=True)
            ]
            self.assertNotIn("run_dreamina_canvas", default_tools)
            explicit_tools = [
                t.get("function", {}).get("name")
                for t in orch._build_tools(is_deepseek_native=True, include_dreamina=True)
            ]
            self.assertIn("run_dreamina_canvas", explicit_tools)
            store.close()

    async def test_non_admin_beta_user_hard_blocked_zero_spawns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            admin = "+8613800138000"
            member = "+8613900008888"
            cfg = AgentConfig(
                deepseek_api_key="sk-valid-gate-test",
                allowed_senders={admin},
                enable_invite_codes=True,
                enable_instant_ack=False,
                state_dir=tmp_path / "state",
            )
            store = StateStore(cfg.sqlite_db_path)
            store.allow_user(member, note="beta-tester")
            bridge = CliBridge(quiet=True)

            spawns = 0

            async def forbidden_runner(
                cmd: list[str], cwd: Path, env: dict[str, str], timeout_sec: float
            ) -> tuple[int, str, str]:
                nonlocal spawns
                spawns += 1
                raise AssertionError("Dreamina CLI must never spawn for non-admin!")

            async def forbidden_llm(kwargs: dict[str, Any], call_num: int) -> _MockCompletion:
                raise AssertionError("LLM must never be called on blocked Dreamina request!")

            orch = HyperTextOrchestrator(
                config=cfg,
                store=store,
                bridge=bridge,
                human_gate=HumanGate(),
                llm_client=_MockAsyncOpenAI(forbidden_llm),
                dreamina_client=DreaminaClient(
                    cfg,
                    runner=forbidden_runner,
                    cli_path_override="/opt/homebrew/bin/dreamina-canvas",
                    allow_repo_dotenv_fallback=False,
                ),
            )

            reply = await orch.handle_inbound_message(
                InboundMessage(
                    rowid=1,
                    guid="g-beta-1",
                    sender=member,
                    text="/即梦 画一只橘猫",
                    timestamp_unix=time.time(),
                )
            )
            self.assertIn("仅限主理人本人调用", reply or "")
            self.assertEqual(spawns, 0)

            tool_raw = await orch._execute_local_tool(
                member, "run_dreamina_canvas", json.dumps({"prompt": "画一只猫"})
            )
            tool_obj = json.loads(tool_raw)
            self.assertFalse(tool_obj["ok"])
            self.assertIn("仅限主理人本人调用", tool_obj["error"])
            self.assertEqual(spawns, 0)
            store.close()

    async def test_self_chat_slash_triggers_and_four_layer_antiloop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            chat_db = tmp_path / "chat.db"
            _init_mock_chat_db(chat_db)
            admin = "+8613800138000"

            conn = sqlite3.connect(str(chat_db))
            conn.execute("INSERT INTO handle (ROWID, id) VALUES (1, ?)", (admin,))
            conn.execute(
                "INSERT INTO chat (ROWID, guid, chat_identifier) VALUES (1, ?, ?)",
                (f"iMessage;-;{admin}", admin),
            )
            conn.commit()
            conn.close()

            cfg = AgentConfig(
                deepseek_api_key="sk-valid-selfchat",
                allowed_senders={normalize_sender(admin)},
                allow_self_chat=True,
                self_chat_prefix="/ai",
                chat_db_path=chat_db,
                state_dir=tmp_path / "state",
            )
            store = StateStore(cfg.sqlite_db_path)
            bridge = MacIMessageBridge(config=cfg, store=store, human_gate=HumanGate())
            bridge.align_cursor_to_latest()

            conn = sqlite3.connect(str(chat_db))
            # 1+2: Twin rows for `/即梦 画海报` -> must emit exactly 1 InboundMessage preserving `/即梦`
            conn.execute(
                "INSERT INTO message (ROWID, guid, text, handle_id, is_from_me) VALUES (1, 'm1', '/即梦 画海报', 1, 1)"
            )
            conn.execute("INSERT INTO chat_message_join VALUES (1, 1)")
            conn.execute(
                "INSERT INTO message (ROWID, guid, text, handle_id, is_from_me) VALUES (2, 'm2', '/即梦 画海报', 1, 0)"
            )
            conn.execute("INSERT INTO chat_message_join VALUES (1, 2)")
            # 3+4: Word-boundary rejects `/jmbot` and `/dreaminabot`
            conn.execute(
                "INSERT INTO message (ROWID, guid, text, handle_id, is_from_me) VALUES (3, 'm3', '/jmbot hello', 1, 1)"
            )
            conn.execute("INSERT INTO chat_message_join VALUES (1, 3)")
            conn.execute(
                "INSERT INTO message (ROWID, guid, text, handle_id, is_from_me) VALUES (4, 'm4', '/dreaminabot hi', 1, 1)"
            )
            conn.execute("INSERT INTO chat_message_join VALUES (1, 4)")
            # 5: `/jm` valid trigger
            conn.execute(
                "INSERT INTO message (ROWID, guid, text, handle_id, is_from_me) VALUES (5, 'm5', '/jm 城市日落', 1, 1)"
            )
            conn.execute("INSERT INTO chat_message_join VALUES (1, 5)")
            conn.commit()
            conn.close()

            polled = bridge.poll_new_messages()
            self.assertEqual([m.text for m in polled], ["/即梦 画海报", "/jm 城市日落"])

            # Verify Layer-1 outbound echo suppression prevents infinite loops
            bridge.record_outbound_echo("🎨 已重置即梦画布上下文，下一条生成指令将自动创建全新画布。")
            conn = sqlite3.connect(str(chat_db))
            conn.execute(
                "INSERT INTO message (ROWID, guid, text, handle_id, is_from_me) VALUES (6, 'm6', '🎨 已重置即梦画布上下文，下一条生成指令将自动创建全新画布。', 1, 1)"
            )
            conn.execute("INSERT INTO chat_message_join VALUES (1, 6)")
            conn.commit()
            conn.close()
            self.assertEqual(bridge.poll_new_messages(), [])
            store.close()

    async def test_auth_refresh_recovery_and_login_hint_when_expired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg = AgentConfig(state_dir=tmp_path / "state")
            store = StateStore(cfg.sqlite_db_path)

            # Scenario A: auth status false -> auth refresh fails -> friendly login hint
            async def unauth_runner(
                cmd: list[str], cwd: Path, env: dict[str, str], timeout_sec: float
            ) -> tuple[int, str, str]:
                if cmd[1:3] in (["auth", "status"], ["auth", "refresh"]):
                    return (
                        0,
                        json.dumps({"schemaVersion": "1", "ok": True, "data": {"loggedIn": False}}),
                        "",
                    )
                raise AssertionError(f"Should not run {cmd} when unauthenticated")

            client_unauth = DreaminaClient(
                cfg,
                runner=unauth_runner,
                cli_path_override="/opt/homebrew/bin/dreamina-canvas",
                allow_repo_dotenv_fallback=False,
            )
            res_unauth = await client_unauth.ask("画一只橘猫", sender="+8613800138000", store=store)
            self.assertFalse(res_unauth.ok)
            self.assertIn("dreamina-canvas auth login", res_unauth.formatted_reply)

            # Scenario B: auth status false -> auth refresh succeeds (`loggedIn: true`) -> generates cleanly
            seen_steps: list[list[str]] = []

            async def refresh_ok_runner(
                cmd: list[str], cwd: Path, env: dict[str, str], timeout_sec: float
            ) -> tuple[int, str, str]:
                seen_steps.append(cmd[1:4])
                if cmd[1:3] == ["auth", "status"]:
                    return (
                        0,
                        json.dumps({"schemaVersion": "1", "ok": True, "data": {"loggedIn": False}}),
                        "",
                    )
                if cmd[1:3] == ["auth", "refresh"]:
                    return (
                        0,
                        json.dumps({"schemaVersion": "1", "ok": True, "data": {"loggedIn": True}}),
                        "",
                    )
                if cmd[1:3] == ["canvas", "create"]:
                    return (
                        0,
                        json.dumps(
                            {
                                "schemaVersion": "1",
                                "ok": True,
                                "data": {
                                    "project": {
                                        "projectId": "p_ref_1",
                                        "webUrl": "https://jimeng.jianying.com/ai-tool/canvas/p_ref_1",
                                    }
                                },
                            }
                        ),
                        "",
                    )
                if cmd[1:4] == ["node", "create", "image"]:
                    return (
                        0,
                        json.dumps(
                            {
                                "schemaVersion": "1",
                                "ok": True,
                                "data": {"node": {"nodeId": "n_ref_1"}},
                            }
                        ),
                        "",
                    )
                raise AssertionError(f"Unexpected cmd: {cmd}")

            client_ref = DreaminaClient(
                cfg,
                runner=refresh_ok_runner,
                cli_path_override="/opt/homebrew/bin/dreamina-canvas",
                allow_repo_dotenv_fallback=False,
            )
            res_ok = await client_ref.ask("画一只橘猫", sender="+8613800138000", store=store)
            self.assertTrue(res_ok.ok)
            self.assertIn(["auth", "refresh", "--format"], seen_steps)
            store.close()

    async def test_t2i_inbound_photo_i2i_four_layer_persistence_and_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            admin = "+8613800138000"
            cfg = AgentConfig(
                deepseek_api_key="sk-valid-t2i-i2i",
                allowed_senders={admin},
                enable_instant_ack=False,
                split_standalone_links=True,
                state_dir=tmp_path / "state",
            )
            store = StateStore(cfg.sqlite_db_path)
            bridge = CliBridge(quiet=True)

            inbound_photo = tmp_path / "pet_input.jpg"
            inbound_photo.write_bytes(b"\xff\xd8\xff\xe0inboundjpeg")

            recorded_cmds: list[list[str]] = []
            canvas_creates = 0

            async def runner(
                cmd: list[str], cwd: Path, env: dict[str, str], timeout_sec: float
            ) -> tuple[int, str, str]:
                nonlocal canvas_creates
                recorded_cmds.append(list(cmd))
                if cmd[1:3] == ["auth", "status"]:
                    return (0, json.dumps({"schemaVersion": "1", "ok": True, "data": {"loggedIn": True}}), "")
                if cmd[1:3] == ["canvas", "create"]:
                    canvas_creates += 1
                    pid = f"proj_main_{canvas_creates}"
                    return (
                        0,
                        json.dumps(
                            {
                                "schemaVersion": "1",
                                "ok": True,
                                "data": {
                                    "project": {
                                        "projectId": pid,
                                        "webUrl": f"https://jimeng.jianying.com/ai-tool/canvas/{pid}",
                                    }
                                },
                            }
                        ),
                        "",
                    )
                if cmd[1:3] == ["resource", "upload"]:
                    return (
                        0,
                        json.dumps(
                            {
                                "schemaVersion": "1",
                                "ok": True,
                                "data": {"resource": {"resourceId": "res_up_photo_1"}},
                            }
                        ),
                        "",
                    )
                if cmd[1:4] == ["node", "create", "image"]:
                    mode = cmd[cmd.index("--mode") + 1]
                    nid = f"node_{mode}_1"
                    rid = f"res_{mode}_out"
                    return (
                        0,
                        json.dumps(
                            {
                                "schemaVersion": "1",
                                "ok": True,
                                "data": {
                                    "node": {"nodeId": nid},
                                    "submission": {"resources": [{"resourceId": rid}]},
                                },
                            }
                        ),
                        "",
                    )
                if cmd[1:3] == ["resource", "download"]:
                    rid = cmd[3]
                    out_file = cwd / f"{rid}.png"
                    out_file.write_bytes(b"\x89PNG\r\n\x1a\ngenerated")
                    return (
                        0,
                        json.dumps({"schemaVersion": "1", "ok": True, "data": {"path": str(out_file)}}),
                        "",
                    )
                raise AssertionError(f"Unexpected command: {cmd}")

            dm_client = DreaminaClient(
                cfg,
                runner=runner,
                cli_path_override="/opt/homebrew/bin/dreamina-canvas",
                allow_repo_dotenv_fallback=False,
            )
            orch = HyperTextOrchestrator(
                config=cfg,
                store=store,
                bridge=bridge,
                human_gate=HumanGate(),
                dreamina_client=dm_client,
            )

            # Turn 1: t2i poster -> ratio 3:4, creates canvas, delivers standalone link bubble + PNG attachment
            await orch.handle_inbound_message(
                InboundMessage(
                    rowid=1,
                    guid="t2i-1",
                    sender=admin,
                    text="/即梦 一只戴宇航头盔的橘猫在月球喝咖啡，电影感海报",
                    timestamp_unix=time.time(),
                )
            )
            self.assertEqual(canvas_creates, 1)
            st = dm_client.get_canvas_state(store, admin)
            self.assertEqual(st["project_id"], "proj_main_1")
            self.assertEqual(st["web_url"], "https://jimeng.jianying.com/ai-tool/canvas/proj_main_1")
            self.assertEqual(st["last_node_id"], "node_t2i_1")
            self.assertEqual(st["last_resource_id"], "res_t2i_out")

            # Standalone pure-URL bubble sent for native iOS link card + native attachment sent
            self.assertIn(
                (admin, "https://jimeng.jianying.com/ai-tool/canvas/proj_main_1"),
                bridge.outbox,
            )
            self.assertEqual(len(bridge.attachments), 1)
            self.assertTrue(bridge.attachments[0][1].endswith("res_t2i_out.png"))

            t2i_cmd = next(c for c in recorded_cmds if c[1:4] == ["node", "create", "image"])
            self.assertEqual(t2i_cmd[t2i_cmd.index("--mode") + 1], "t2i")
            self.assertEqual(t2i_cmd[t2i_cmd.index("--ratio") + 1], "3:4")
            self.assertEqual(t2i_cmd[t2i_cmd.index("--model") + 1], "seedream_5.5")

            # Turn 2: Inbound photo i2i on SAME canvas (no second `canvas create`)
            await orch.handle_inbound_message(
                InboundMessage(
                    rowid=2,
                    guid="i2i-2",
                    sender=admin,
                    text="/即梦 把这张照片转成吉卜力水彩插画风格",
                    timestamp_unix=time.time(),
                    attachment_paths=[str(inbound_photo)],
                )
            )
            self.assertEqual(canvas_creates, 1)
            img_cmds = [c for c in recorded_cmds if c[1:4] == ["node", "create", "image"]]
            self.assertEqual(len(img_cmds), 2)
            i2i_cmd = img_cmds[1]
            self.assertEqual(i2i_cmd[i2i_cmd.index("--mode") + 1], "i2i")
            self.assertEqual(i2i_cmd[i2i_cmd.index("--ref") + 1], "res:res_up_photo_1")
            self.assertTrue(
                i2i_cmd[i2i_cmd.index("--prompt") + 1].startswith("{{res:res_up_photo_1}} ")
            )

            # Reset command clears all 4 canvas state keys
            reset_reply = await orch.handle_inbound_message(
                InboundMessage(
                    rowid=3,
                    guid="rst-3",
                    sender=admin,
                    text="/即梦 reset",
                    timestamp_unix=time.time(),
                )
            )
            self.assertIn("已重置即梦画布上下文", reset_reply or "")
            st_after = dm_client.get_canvas_state(store, admin)
            self.assertEqual(
                st_after,
                {"project_id": "", "web_url": "", "last_node_id": "", "last_resource_id": ""},
            )
            store.close()

    async def test_video_reference_modes_t2v_m2v_first_last_frame_and_audio_strict_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg = AgentConfig(state_dir=tmp_path / "state")
            store = StateStore(cfg.sqlite_db_path)
            admin = "+8613800138000"

            img_a = tmp_path / "frame_a.png"
            img_b = tmp_path / "frame_b.png"
            img_c = tmp_path / "frame_c.png"
            for p in (img_a, img_b, img_c):
                p.write_bytes(b"\x89PNG\r\n\x1a\nframe")

            upload_seq = 0
            video_cmds: list[list[str]] = []
            audio_cmds: list[list[str]] = []
            voice_list_calls = 0

            async def runner(
                cmd: list[str], cwd: Path, env: dict[str, str], timeout_sec: float
            ) -> tuple[int, str, str]:
                nonlocal upload_seq, voice_list_calls
                if cmd[1:3] == ["auth", "status"]:
                    return (0, json.dumps({"schemaVersion": "1", "ok": True, "data": {"loggedIn": True}}), "")
                if cmd[1:3] == ["canvas", "create"]:
                    return (
                        0,
                        json.dumps(
                            {
                                "schemaVersion": "1",
                                "ok": True,
                                "data": {
                                    "project": {
                                        "projectId": "proj_va",
                                        "webUrl": "https://jimeng.jianying.com/ai-tool/canvas/proj_va",
                                    }
                                },
                            }
                        ),
                        "",
                    )
                if cmd[1:3] == ["resource", "upload"]:
                    upload_seq += 1
                    return (
                        0,
                        json.dumps(
                            {
                                "schemaVersion": "1",
                                "ok": True,
                                "data": {"resource": {"resourceId": f"rup_{upload_seq}"}},
                            }
                        ),
                        "",
                    )
                if cmd[1:4] == ["node", "create", "video"]:
                    video_cmds.append(list(cmd))
                    return (
                        0,
                        json.dumps(
                            {
                                "schemaVersion": "1",
                                "ok": True,
                                "data": {
                                    "node": {"nodeId": f"nvid_{len(video_cmds)}"},
                                    "submission": {"resources": [{"resourceId": "rvid_stream"}]},
                                },
                            }
                        ),
                        "",
                    )
                if cmd[1:3] == ["voice", "list"]:
                    voice_list_calls += 1
                    return (
                        0,
                        json.dumps(
                            {
                                "schemaVersion": "1",
                                "ok": True,
                                "data": {
                                    "models": {
                                        "seed_tts": {
                                            "items": [
                                                {"voiceName": "磁性男声"},
                                                {"voiceName": "清甜女声"},
                                            ]
                                        }
                                    }
                                },
                            }
                        ),
                        "",
                    )
                if cmd[1:4] == ["node", "create", "audio"]:
                    audio_cmds.append(list(cmd))
                    return (
                        0,
                        json.dumps(
                            {
                                "schemaVersion": "1",
                                "ok": True,
                                "data": {"node": {"nodeId": f"naud_{len(audio_cmds)}"}},
                            }
                        ),
                        "",
                    )
                if cmd[1:3] == ["resource", "download"]:
                    # Simulate video stream restriction where `resource download` fails
                    return (
                        1,
                        json.dumps(
                            {
                                "schemaVersion": "1",
                                "ok": False,
                                "error": {"code": "STREAM_ONLY", "message": "video download disabled"},
                            }
                        ),
                        "",
                    )
                raise AssertionError(f"Unexpected cmd: {cmd}")

            client = DreaminaClient(
                cfg,
                runner=runner,
                cli_path_override="/opt/homebrew/bin/dreamina-canvas",
                allow_repo_dotenv_fallback=False,
            )

            # 1) 0 refs -> `--mode t2v` (and graceful webUrl fallback when download fails!)
            r_t2v = await client.ask("生成一段 6 秒未来城市航拍视频", sender=admin, store=store)
            self.assertTrue(r_t2v.ok)
            self.assertEqual(r_t2v.mode, "t2v")
            self.assertEqual(r_t2v.deliverable_paths, [])
            self.assertIn("https://jimeng.jianying.com/ai-tool/canvas/proj_va", r_t2v.formatted_reply)
            self.assertNotIn("--ref", video_cmds[0])
            self.assertEqual(video_cmds[0][video_cmds[0].index("--duration") + 1], "6")

            # 2) 1 uploaded image -> `--mode m2v --ref res:rup_1` (NEVER first_last_frame!)
            r_m2v_1 = await client.ask(
                "让画面里的海浪轻轻涌动做成视频",
                sender=admin,
                store=store,
                attachment_paths=[img_a],
            )
            self.assertTrue(r_m2v_1.ok)
            self.assertEqual(r_m2v_1.mode, "m2v")
            c_m2v = video_cmds[1]
            self.assertEqual(c_m2v[c_m2v.index("--mode") + 1], "m2v")
            self.assertEqual(c_m2v[c_m2v.index("--ref") + 1], "res:rup_1")
            self.assertIn("{{res:rup_1}}", c_m2v[c_m2v.index("--prompt") + 1])

            # 3) Exactly 2 uploaded images -> `--mode first_last_frame --ref res:rup_2 --ref res:rup_3`
            r_flf = await client.ask(
                "从白天平滑过渡到夜晚的延时视频",
                sender=admin,
                store=store,
                attachment_paths=[img_a, img_b],
            )
            self.assertTrue(r_flf.ok)
            self.assertEqual(r_flf.mode, "first_last_frame")
            c_flf = video_cmds[2]
            self.assertEqual(c_flf[c_flf.index("--mode") + 1], "first_last_frame")
            ref_vals = [c_flf[i + 1] for i, tok in enumerate(c_flf) if tok == "--ref"]
            self.assertEqual(ref_vals, ["res:rup_2", "res:rup_3"])
            self.assertIn("{{res:rup_2}} {{res:rup_3}}", c_flf[c_flf.index("--prompt") + 1])

            # 4) 3 uploaded images (>2) -> `--mode m2v`
            r_m2v_3 = await client.ask(
                "融合这三张设定图生成一段角色登场视频",
                sender=admin,
                store=store,
                attachment_paths=[img_a, img_b, img_c],
            )
            self.assertTrue(r_m2v_3.ok)
            self.assertEqual(r_m2v_3.mode, "m2v")

            # 5) Audio `music`: MUST pass `--model seed_music_1.0 --duration 45`, NEVER `--voice-name`
            r_music = await client.ask("生成一段 45 秒温暖木吉他背景音乐", sender=admin, store=store)
            self.assertTrue(r_music.ok)
            self.assertEqual(r_music.mode, "music")
            c_music = audio_cmds[0]
            self.assertEqual(c_music[c_music.index("--mode") + 1], "music")
            self.assertEqual(c_music[c_music.index("--model") + 1], "seed_music_1.0")
            self.assertEqual(c_music[c_music.index("--duration") + 1], "45")
            self.assertNotIn("--voice-name", c_music)

            # 6) Audio `tts`: MUST query `voice list`, pass `--voice-name`, and NEVER pass `--model` or `--duration`
            r_tts = await client.ask("配音：欢迎体验超文本本地创作室", sender=admin, store=store)
            self.assertTrue(r_tts.ok)
            self.assertEqual(r_tts.mode, "tts")
            self.assertEqual(voice_list_calls, 1)
            c_tts = audio_cmds[1]
            self.assertEqual(c_tts[c_tts.index("--mode") + 1], "tts")
            self.assertEqual(c_tts[c_tts.index("--voice-name") + 1], "磁性男声")
            self.assertNotIn("--model", c_tts)
            self.assertNotIn("--duration", c_tts)
            store.close()

    async def test_model_fallback_chains_happyhorse_720P_and_multi_agent_arena(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            admin = "+8613800138000"
            cfg = AgentConfig(
                deepseek_api_key="sk-valid-arena",
                allowed_senders={admin},
                enable_instant_ack=False,
                state_dir=tmp_path / "state",
            )
            store = StateStore(cfg.sqlite_db_path)
            bridge = CliBridge(quiet=True)

            # Verify instant ack crafting for single vs multi-agent Dreamina
            self.assertEqual(
                HyperTextOrchestrator._craft_instant_ack("/即梦 画一只橘猫"),
                "收到，正在用即梦画布生成，好了直接发你。",
            )
            self.assertEqual(
                HyperTextOrchestrator._craft_instant_ack("让 Codex 写口号并用即梦画海报"),
                "收到，我调起 Codex、即梦画布 处理一下，好了回你。",
            )

            seen_img_models: list[tuple[str, str]] = []
            seen_vid_models: list[tuple[str, str]] = []

            async def dm_runner(
                cmd: list[str], cwd: Path, env: dict[str, str], timeout_sec: float
            ) -> tuple[int, str, str]:
                if cmd[1:3] == ["auth", "status"]:
                    return (0, json.dumps({"schemaVersion": "1", "ok": True, "data": {"loggedIn": True}}), "")
                if cmd[1:3] == ["canvas", "create"]:
                    return (
                        0,
                        json.dumps(
                            {
                                "schemaVersion": "1",
                                "ok": True,
                                "data": {
                                    "project": {
                                        "projectId": "p_fb",
                                        "webUrl": "https://jimeng.jianying.com/ai-tool/canvas/p_fb",
                                    }
                                },
                            }
                        ),
                        "",
                    )
                if cmd[1:4] == ["node", "create", "image"]:
                    m = cmd[cmd.index("--model") + 1]
                    r = cmd[cmd.index("--resolution") + 1]
                    seen_img_models.append((m, r))
                    if m != "high_aes_general_v50_flash":
                        return (
                            1,
                            json.dumps(
                                {
                                    "schemaVersion": "1",
                                    "ok": False,
                                    "error": {"code": "VIP_REQUIRED", "message": "model requires vip"},
                                }
                            ),
                            "",
                        )
                    return (
                        0,
                        json.dumps(
                            {
                                "schemaVersion": "1",
                                "ok": True,
                                "data": {
                                    "node": {"nodeId": "n_fb_img"},
                                    "submission": {"resources": [{"resourceId": "r_fb_img"}]},
                                },
                            }
                        ),
                        "",
                    )
                if cmd[1:4] == ["node", "create", "video"]:
                    m = cmd[cmd.index("--model") + 1]
                    r = cmd[cmd.index("--resolution") + 1]
                    seen_vid_models.append((m, r))
                    if m != "happyhorse_1.1":
                        return (
                            1,
                            json.dumps(
                                {
                                    "schemaVersion": "1",
                                    "ok": False,
                                    "error": {"code": "MODEL_BUSY", "message": "capacity full"},
                                }
                            ),
                            "",
                        )
                    return (
                        0,
                        json.dumps(
                            {
                                "schemaVersion": "1",
                                "ok": True,
                                "data": {"node": {"nodeId": "n_fb_vid"}},
                            }
                        ),
                        "",
                    )
                if cmd[1:3] == ["resource", "download"]:
                    out_f = cwd / "arena_poster.png"
                    out_f.write_bytes(b"\x89PNG\r\n\x1a\narena")
                    return (
                        0,
                        json.dumps({"schemaVersion": "1", "ok": True, "data": {"path": str(out_f)}}),
                        "",
                    )
                raise AssertionError(f"Unexpected cmd: {cmd}")

            dm_client = DreaminaClient(
                cfg,
                runner=dm_runner,
                cli_path_override="/opt/homebrew/bin/dreamina-canvas",
                allow_repo_dotenv_fallback=False,
            )

            # Check video fallback chain reaches happyhorse_1.1 with uppercase "720P"
            vid_res = await dm_client.ask("生成一段 5 秒星空延时视频", sender=admin, store=store)
            self.assertTrue(vid_res.ok)
            self.assertEqual(
                seen_vid_models,
                [
                    ("seedance_2.5", "720p"),
                    ("seedance_2.5_flash", "720p"),
                    ("seedance_pro_fast", "720p"),
                    ("happyhorse_1.1", "720P"),
                ],
            )

            # Multi-agent arena: Codex + Dreamina orchestrated in a single turn
            async def cdx_runner(
                cmd: list[str], cwd: Path, env: dict[str, str], timeout_sec: float
            ) -> tuple[int, str, str]:
                out_idx = cmd.index("-o")
                Path(cmd[out_idx + 1]).write_text("口号：无感连接，灵感即刻成片。", encoding="utf-8")
                return (0, '{"type":"thread.started","thread_id":"t_arena"}\n', "")

            async def arena_llm(kwargs: dict[str, Any], call_num: int) -> _MockCompletion:
                tool_names = [
                    t["function"]["name"]
                    for t in kwargs["tools"]
                    if t.get("type") == "function"
                ]
                self.assertIn("ask_codex", tool_names)
                self.assertIn("run_dreamina_canvas", tool_names)
                if call_num == 1:
                    return _MockCompletion(
                        choices=[
                            _MockChoice(
                                message=_MockMessage(
                                    content="",
                                    tool_calls=[
                                        _MockToolCall(
                                            id="tc_cdx",
                                            function=_MockFunction(
                                                name="ask_codex",
                                                arguments=json.dumps({"message": "写一句发布会口号"}),
                                            ),
                                        ),
                                        _MockToolCall(
                                            id="tc_dm",
                                            function=_MockFunction(
                                                name="run_dreamina_canvas",
                                                arguments=json.dumps(
                                                    {
                                                        "prompt": "极简未来感发布会主视觉海报",
                                                        "media_type": "image",
                                                        "ratio": "3:4",
                                                    }
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
                                content=(
                                    "口号与主视觉海报都准备好啦：无感连接，灵感即刻成片。\n\n"
                                    "https://jimeng.jianying.com/ai-tool/canvas/p_fb"
                                )
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
                codex_client=CodexClient(
                    cfg,
                    runner=cdx_runner,
                    cli_path_override="/opt/homebrew/bin/codex",
                    proxy_probe=lambda h, p: False,
                    allow_repo_dotenv_fallback=False,
                ),
                dreamina_client=dm_client,
            )

            arena_ans = await orch.handle_inbound_message(
                InboundMessage(
                    rowid=10,
                    guid="arena-10",
                    sender=admin,
                    text="让 Codex 写一句产品发布会口号，再用即梦画一张配套主视觉海报",
                    timestamp_unix=time.time(),
                )
            )
            self.assertIn("无感连接，灵感即刻成片", arena_ans or "")
            self.assertEqual(
                seen_img_models,
                [
                    ("seedream_5.5", "2K"),
                    ("seedream_5.0_pro", "2K"),
                    ("high_aes_general_v50_flash", "1.5K"),
                ],
            )
            self.assertTrue(any(att[1].endswith("arena_poster.png") for att in bridge.attachments))
            store.close()


if __name__ == "__main__":
    unittest.main()
