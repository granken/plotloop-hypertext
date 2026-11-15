"""Comprehensive offline self-test suite verifying all 10 HyperText v0.5.0 subsystems."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bridge_imessage import (
    BOT_ECHO_MARKER,
    CliBridge,
    InboundMessage,
    MacIMessageBridge,
    decode_attributed_body,
    encode_attributed_body,
)
from .config import AgentConfig
from .human_gate import HumanGate
from .orchestrator import HyperTextOrchestrator, InstinctOrchestrator
from .sandbox_browser import BrowserSandbox
from .store_sqlite import StateStore


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


class _MockCompletionsAPI:
    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _MockCompletion:
        snapshot = json.loads(json.dumps(kwargs, ensure_ascii=False))
        self.calls.append(snapshot)
        return await self._handler(kwargs, len(self.calls))


class _MockChatNamespace:
    def __init__(self, handler: Any) -> None:
        self.completions = _MockCompletionsAPI(handler)


class _MockAsyncOpenAI:
    def __init__(self, handler: Any) -> None:
        self.chat = _MockChatNamespace(handler)


def _check_attributed_body() -> None:
    short_text = "帮我查一下明天上午去上海虹桥的高铁票"
    short_blob = encode_attributed_body("\ufffc" + short_text + "\ufffc")
    assert len(short_text.encode("utf-8")) < 0x80
    assert decode_attributed_body(short_blob) == short_text

    med_text = "这是一段超过128字节的中文长消息测试：" + ("星巴克冰美式少冰靠窗座位_" * 8)
    assert 128 < len(med_text.encode("utf-8")) < 65535
    med_blob = encode_attributed_body(med_text, class_name="NSMutableString")
    assert b"\x81" in med_blob
    assert decode_attributed_body(med_blob) == med_text

    long_text = "超长文本0x82边界验证_" + ("DeepSeekV4Flash百万上下文多模态测试_" * 1600)
    assert len(long_text.encode("utf-8")) > 65535
    long_blob = encode_attributed_body("\ufffc" + long_text)
    assert b"\x82" in long_blob[:120]
    assert decode_attributed_body(long_blob) == long_text


def _check_chat_db_readonly_and_filtering(tmp_dir: Path) -> None:
    fake_chat_db = tmp_dir / "chat.db"
    conn = sqlite3.connect(str(fake_chat_db))
    conn.executescript(
        """
        CREATE TABLE handle (
            ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL
        );
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
            guid TEXT,
            handle_id INTEGER,
            text TEXT,
            attributedBody BLOB,
            date INTEGER,
            is_from_me INTEGER DEFAULT 0,
            associated_message_type INTEGER DEFAULT 0
        );
        INSERT INTO handle(ROWID, id) VALUES (1, '+8613800138000');
        INSERT INTO handle(ROWID, id) VALUES (2, '+8619999999999');
        INSERT INTO message(ROWID, guid, handle_id, text, date, is_from_me, associated_message_type)
            VALUES (1, 'g-old', 1, '历史旧消息不应重放', 750000000000000000, 0, 0);
        """
    )
    conn.commit()

    store = StateStore(tmp_dir / "state_bridge.sqlite3")
    bridge = MacIMessageBridge(
        chat_db_path=fake_chat_db,
        store=store,
        allowed_senders={"+8613800138000"},
    )
    assert bridge.align_cursor_to_latest() == 1
    assert bridge.poll_inbound() == []

    with bridge._connect_readonly() as ro_conn:
        try:
            ro_conn.execute("DELETE FROM message")
            raise AssertionError("Expected sqlite3.OperationalError on write to mode=ro chat.db")
        except sqlite3.OperationalError:
            pass

    attr_blob = encode_attributed_body("\ufffc请帮我订一杯虹桥T2冰美式")
    conn.execute(
        "INSERT INTO message(ROWID, guid, handle_id, text, date, is_from_me, associated_message_type) VALUES (2, 'g-tap', 1, 'Loved “hi”', 750000001000000000, 0, 2000)"
    )
    conn.execute(
        "INSERT INTO message(ROWID, guid, handle_id, text, date, is_from_me, associated_message_type) VALUES (3, 'g-stranger', 2, '陌生人消息', 750000002000000000, 0, 0)"
    )
    conn.execute(
        "INSERT INTO message(ROWID, guid, handle_id, text, date, is_from_me, associated_message_type) VALUES (4, 'g-me', 1, '我自己的消息', 750000003000000000, 1, 0)"
    )
    conn.execute(
        "INSERT INTO message(ROWID, guid, handle_id, text, attributedBody, date, is_from_me, associated_message_type) VALUES (5, 'g-ok', 1, NULL, ?, 750000004000000000, 0, 0)",
        (attr_blob,),
    )
    conn.commit()
    conn.close()

    polled = bridge.poll_inbound()
    assert len(polled) == 1, f"Expected 1 filtered message, got {len(polled)}"
    assert polled[0].rowid == 5
    assert polled[0].text == "请帮我订一杯虹桥T2冰美式"
    assert bridge.poll_inbound() == []
    store.close()


def _check_fts5_hybrid_memory(tmp_dir: Path) -> None:
    store = StateStore(tmp_dir / "state_mem.sqlite3")
    sender = "+8613800138000"

    store.save_memory(
        sender,
        "airport_coffee",
        "每次从上海虹桥机场T2出发必点 Starbucks 冰美式少冰",
        tags="出行,咖啡,虹桥",
    )
    store.save_memory(
        sender,
        "train_seat",
        "高铁二等座与一等座都只选靠窗F座，安静办公",
        tags="高铁,座位",
    )
    store.save_memory(
        sender,
        "hotel_pillow",
        "住酒店需要乳胶枕和无烟高楼层房间",
        tags="酒店,睡眠",
    )

    hits_coffee = store.search_memories(sender, "虹桥 冰美式", limit=5)
    assert hits_coffee and hits_coffee[0]["key"] == "airport_coffee"

    hits_window = store.search_memories(sender, "靠窗", limit=5)
    assert hits_window and hits_window[0]["key"] == "train_seat"

    hits_en = store.search_memories(sender, "starb", limit=5)
    assert hits_en and hits_en[0]["key"] == "airport_coffee"

    store.save_memory(
        sender,
        "airport_coffee",
        "上海虹桥T2现在改喝燕麦拿铁热饮",
        tags="出行,咖啡",
    )
    hits_updated = store.search_memories(sender, "燕麦拿铁", limit=5)
    assert hits_updated and hits_updated[0]["key"] == "airport_coffee"

    assert store.delete_memory(sender, "hotel_pillow") is True
    assert store.delete_memory(sender, "hotel_pillow") is False
    hits_deleted = store.search_memories(sender, "乳胶枕", limit=5)
    assert hits_deleted == []
    store.close()


async def _check_deepseek_and_openai_isolation(tmp_dir: Path) -> None:
    sender = "+8613800138000"
    store = StateStore(tmp_dir / "state_orch.sqlite3")
    bridge = CliBridge(quiet=True)
    gate = HumanGate(default_timeout=10.0)

    store.append_conversation_message(sender, "user", "上一次崩溃前的请求")
    store.append_conversation_message(
        sender,
        "assistant",
        "",
        tool_calls=[
            {
                "id": "crashed_tc_1",
                "type": "function",
                "function": {"name": "search_memory", "arguments": '{"query": "测试"}'},
            }
        ],
        reasoning_content="未完成的思考",
    )

    async def ds_handler(kwargs: dict[str, Any], call_num: int) -> _MockCompletion:
        if call_num == 1:
            return _MockCompletion(
                choices=[
                    _MockChoice(
                        message=_MockMessage(
                            content="",
                            reasoning_content="先检索用户记忆并结合联网结果。",
                            server_tool_calls=[
                                {
                                    "id": "srv_1",
                                    "type": "deepseek_web_search",
                                    "query": "上海虹桥天气",
                                }
                            ],
                            tool_calls=[
                                _MockToolCall(
                                    id="call_mem_1",
                                    function=_MockFunction(
                                        name="save_memory",
                                        arguments='{"key":"weather_city","value":"常驻上海"}',
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
                    message=_MockMessage(
                        content="已记住您常驻上海，虹桥今日晴。",
                        reasoning_content="",
                    )
                )
            ]
        )

    ds_client = _MockAsyncOpenAI(ds_handler)
    ds_config = AgentConfig(
        provider_mode="deepseek",
        deepseek_api_key="sk-valid-test-deepseek-key-123456",
        deepseek_model="deepseek-flash",
        deepseek_reasoning_effort="high",
        enable_deepseek_web_tools=True,
        allowed_senders={sender},
        state_dir=tmp_dir,
    )
    orch_ds = HyperTextOrchestrator(
        config=ds_config,
        store=store,
        bridge=bridge,
        human_gate=gate,
        llm_client=ds_client,
    )

    reply1 = await orch_ds.handle_inbound_message(
        InboundMessage(
            rowid=10,
            guid="g-10",
            sender=sender,
            text="记住我常驻上海并查虹桥天气",
            timestamp_unix=time.time(),
        )
    )
    assert "常驻上海" in (reply1 or "")
    assert len(ds_client.chat.completions.calls) == 2

    call1 = ds_client.chat.completions.calls[0]
    call2 = ds_client.chat.completions.calls[1]
    assert call1["model"] == "deepseek-flash"
    assert call1["reasoning_effort"] == "high"
    assert call1["extra_body"] == {"thinking": {"type": "enabled"}}
    tool_types = [t.get("type") for t in call1["tools"]]
    assert "deepseek_web_search" in tool_types
    assert "deepseek_read_url" in tool_types
    fn_names_ds = [
        t["function"]["name"] for t in call1["tools"] if t.get("type") == "function"
    ]
    assert "fetch_url_text" not in fn_names_ds
    assert "delete_memory" in fn_names_ds

    for m in call1["messages"]:
        if m.get("tool_calls"):
            for tc in m["tool_calls"]:
                assert tc["id"] != "crashed_tc_1"

    assistant_turns_call2 = [m for m in call2["messages"] if m["role"] == "assistant"]
    assert assistant_turns_call2
    last_asst = assistant_turns_call2[-1]
    assert last_asst["reasoning_content"] == "先检索用户记忆并结合联网结果。"
    assert last_asst["server_tool_calls"][0]["type"] == "deepseek_web_search"

    async def oa_handler(kwargs: dict[str, Any], call_num: int) -> _MockCompletion:
        return _MockCompletion(
            choices=[_MockChoice(message=_MockMessage(content="OpenAI 兼容模式回复正常。"))]
        )

    oa_client = _MockAsyncOpenAI(oa_handler)
    oa_config = AgentConfig(
        provider_mode="openai",
        openai_api_key="sk-valid-test-openai-key-123456",
        openai_model="gpt-4.1-mini",
        allowed_senders={sender},
        state_dir=tmp_dir,
    )
    orch_oa = HyperTextOrchestrator(
        config=oa_config,
        store=store,
        bridge=bridge,
        human_gate=gate,
        llm_client=oa_client,
    )
    reply2 = await orch_oa.handle_inbound_message(
        InboundMessage(
            rowid=11,
            guid="g-11",
            sender=sender,
            text="测试切换兼容模式",
            timestamp_unix=time.time(),
        )
    )
    assert "OpenAI" in (reply2 or "")
    oa_call = oa_client.chat.completions.calls[0]
    assert "extra_body" not in oa_call
    assert "reasoning_effort" not in oa_call
    oa_tool_types = [t.get("type") for t in oa_call["tools"]]
    assert "deepseek_web_search" not in oa_tool_types
    assert "deepseek_read_url" not in oa_tool_types
    oa_fn_names = [
        t["function"]["name"] for t in oa_call["tools"] if t.get("type") == "function"
    ]
    assert "fetch_url_text" in oa_fn_names
    for m in oa_call["messages"]:
        assert "reasoning_content" not in m
        assert "server_tool_calls" not in m
    assert any(m.get("tool_calls") for m in oa_call["messages"] if m["role"] == "assistant")
    assert any(m["role"] == "tool" for m in oa_call["messages"])

    store.close()


async def _check_human_gate_2fa_no_deadlock(tmp_dir: Path) -> None:
    sender = "+8613800138000"
    store = StateStore(tmp_dir / "state_2fa.sqlite3")
    bridge = CliBridge(quiet=True)
    gate = HumanGate(default_timeout=5.0)

    async def gate_handler(kwargs: dict[str, Any], call_num: int) -> _MockCompletion:
        if call_num == 1:
            return _MockCompletion(
                choices=[
                    _MockChoice(
                        message=_MockMessage(
                            content="",
                            reasoning_content="需要短信验证码解锁登录",
                            tool_calls=[
                                _MockToolCall(
                                    id="call_2fa",
                                    function=_MockFunction(
                                        name="ask_human_confirmation",
                                        arguments='{"question":"请输入航司6位短信验证码"}',
                                    ),
                                )
                            ],
                        )
                    )
                ]
            )
        tool_msgs = [m for m in kwargs["messages"] if m["role"] == "tool"]
        assert any("839201" in m["content"] for m in tool_msgs)
        return _MockCompletion(
            choices=[
                _MockChoice(
                    message=_MockMessage(
                        content="验证码 839201 核验通过，积分查询完成。",
                        reasoning_content="",
                    )
                )
            ]
        )

    orch = HyperTextOrchestrator(
        config=AgentConfig(
            provider_mode="deepseek",
            deepseek_api_key="sk-valid-test-key-2fa",
            allowed_senders={sender},
            state_dir=tmp_dir,
        ),
        store=store,
        bridge=bridge,
        human_gate=gate,
        llm_client=_MockAsyncOpenAI(gate_handler),
    )

    t1 = asyncio.create_task(
        orch.handle_inbound_message(
            InboundMessage(
                rowid=20,
                guid="g-20",
                sender=sender,
                text="帮我登录东方航空查里程",
                timestamp_unix=time.time(),
            )
        )
    )

    for _ in range(50):
        if gate.has_pending(sender):
            break
        await asyncio.sleep(0.02)
    assert gate.has_pending(sender), "HumanGate did not enter pending state"

    t2 = asyncio.create_task(
        orch.handle_inbound_message(
            InboundMessage(
                rowid=21,
                guid="g-21",
                sender=sender,
                text="839201",
                timestamp_unix=time.time(),
            )
        )
    )
    res2 = await asyncio.wait_for(t2, timeout=2.0)
    assert res2 is None
    res1 = await asyncio.wait_for(t1, timeout=2.0)
    assert "839201" in (res1 or "")
    store.close()


async def _check_proactive_cron(tmp_dir: Path) -> None:
    sender = "+8613800138000"
    store = StateStore(tmp_dir / "state_cron.sqlite3")
    bridge = CliBridge(quiet=True)
    gate = HumanGate(default_timeout=5.0)

    async def cron_llm_handler(kwargs: dict[str, Any], call_num: int) -> _MockCompletion:
        return _MockCompletion(
            choices=[
                _MockChoice(
                    message=_MockMessage(
                        content="早安简报：今日日程包含上午10点产品评审。",
                        reasoning_content="",
                    )
                )
            ]
        )

    orch = HyperTextOrchestrator(
        config=AgentConfig(
            provider_mode="deepseek",
            deepseek_api_key="sk-valid-test-key-cron",
            allowed_senders={sender},
            state_dir=tmp_dir,
        ),
        store=store,
        bridge=bridge,
        human_gate=gate,
        llm_client=_MockAsyncOpenAI(cron_llm_handler),
    )

    now = time.time()
    store.add_scheduled_task(sender, "direct_notify", "该喝水休息5分钟了", run_at=now - 5)
    store.add_scheduled_task(sender, "agent_wakeup", "生成今日早安简报", run_at=now - 2)

    executed = await orch.process_due_tasks_once(now_ts=now)
    assert executed == 2
    assert len(bridge.outbox) == 2
    assert "该喝水休息5分钟了" in bridge.outbox[0][1]
    assert "早安简报" in bridge.outbox[1][1]
    assert await orch.process_due_tasks_once(now_ts=now + 10) == 0
    store.close()


async def _check_same_apple_id_self_chat(tmp_dir: Path) -> None:
    self_db = tmp_dir / "self_chat.db"
    conn = sqlite3.connect(str(self_db))
    conn.executescript(
        """
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL);
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, chat_identifier TEXT NOT NULL);
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
            guid TEXT,
            handle_id INTEGER,
            text TEXT,
            attributedBody BLOB,
            cache_has_attachments INTEGER DEFAULT 0,
            date INTEGER,
            is_from_me INTEGER DEFAULT 0,
            associated_message_type INTEGER DEFAULT 0
        );
        INSERT INTO handle(ROWID, id) VALUES (1, '+8613800138000');
        INSERT INTO handle(ROWID, id) VALUES (2, '+8615555555555');
        INSERT INTO chat(ROWID, chat_identifier) VALUES (1, '+8613800138000');
        """
    )
    conn.commit()

    store = StateStore(tmp_dir / "state_self_chat.sqlite3")
    gate = HumanGate(default_timeout=5.0)
    cfg = AgentConfig(
        allowed_senders={"+8613800138000"},
        allow_self_chat=True,
        self_chat_prefix="/ai",
        state_dir=tmp_dir,
        chat_db_path=self_db,
    )
    bridge = MacIMessageBridge(config=cfg, store=store, human_gate=gate)
    assert bridge.align_cursor_to_latest() == 0

    # Record bot outbound echo & insert:
    # 1-2: plain self-note "买牛奶" (is_from_me=1 & mirror is_from_me=0) -> both ignored
    # 3: is_from_me=1 message sent to ANOTHER friend (/ai 吃饭吗) -> ignored
    # 4-5: "/ai 查上海天气" twin rows (is_from_me=1 & is_from_me=0) -> emitted ONCE as "查上海天气"
    # 6-7: bot echo rows (with \u2063 and via SHA-256 cache) -> both suppressed
    bridge.record_outbound_echo("今日上海多云转晴 22°C")
    conn.executemany(
        "INSERT INTO message(ROWID, guid, handle_id, text, is_from_me, associated_message_type) VALUES (?, ?, ?, ?, ?, 0)",
        [
            (1, "s1", 1, "买牛奶", 1),
            (2, "s2", 1, "买牛奶", 0),
            (3, "s3", 2, "/ai 晚上一起吃饭吗", 1),
            (4, "s4", 1, "/ai 查上海天气", 1),
            (5, "s5", 1, "/ai 查上海天气", 0),
            (6, "s6", 1, f"{BOT_ECHO_MARKER}/ai 这是一条带隐形标记的机器人回复", 1),
            (7, "s7", 1, "今日上海多云转晴 22°C", 0),
        ],
    )
    conn.commit()

    emitted = bridge.poll_new_messages()
    assert len(emitted) == 1, f"Expected exactly 1 deduplicated self-chat message, got {len(emitted)}"
    assert emitted[0].text == "查上海天气"

    # Bare 2FA code without /ai must unlock pending HumanGate once (deduplicating mirror row)
    req_task = asyncio.create_task(gate.request_input("+8613800138000", "输入验证码", bridge=None, timeout=3.0))
    await asyncio.sleep(0.02)
    assert gate.has_pending("+8613800138000")

    conn.executemany(
        "INSERT INTO message(ROWID, guid, handle_id, text, is_from_me, associated_message_type) VALUES (?, ?, ?, ?, ?, 0)",
        [
            (8, "s8", 1, "654321", 1),
            (9, "s9", 1, "654321", 0),
        ],
    )
    conn.commit()
    conn.close()

    emitted_2fa = bridge.poll_new_messages()
    assert len(emitted_2fa) == 1
    assert emitted_2fa[0].text == "654321"
    assert gate.try_resolve(emitted_2fa[0].sender, emitted_2fa[0].text) is True
    assert await req_task == "654321"
    store.close()


async def _check_multiuser_invites_quotas_and_browser_isolation(tmp_dir: Path) -> None:
    store = StateStore(tmp_dir / "state_multi.sqlite3")
    bridge = CliBridge(quiet=True)
    gate = HumanGate(default_timeout=5.0)
    admin = "+8613800138000"
    member = "+8613900001111"
    stranger = "+8613700002222"

    cfg = AgentConfig(
        provider_mode="deepseek",
        deepseek_api_key="sk-valid-multiuser-test-key",
        allowed_senders={admin},
        allow_self_chat=True,
        enable_invite_codes=True,
        default_member_daily_msg_limit=2,
        default_member_daily_browser_limit=1,
        browser_max_concurrency=2,
        state_dir=tmp_dir,
    )
    sandbox = BrowserSandbox(config=cfg, human_gate=gate, bridge=bridge)
    p_admin = sandbox.profile_dir_for(admin)
    p_member = sandbox.profile_dir_for(member)
    assert p_admin != p_member
    assert admin not in str(p_admin) and "13800138000" not in str(p_admin)

    assert InstinctOrchestrator is HyperTextOrchestrator
    code = store.create_invite_code(max_uses=1, daily_msg_limit=2, daily_browser_limit=1, note="beta1")
    assert code.startswith("HT-")

    async def ok_llm(kwargs: dict[str, Any], call_num: int) -> _MockCompletion:
        return _MockCompletion(
            choices=[_MockChoice(message=_MockMessage(content="内测成员回复成功", reasoning_content=""))]
        )

    orch = HyperTextOrchestrator(
        config=cfg,
        store=store,
        bridge=bridge,
        human_gate=gate,
        browser_sandbox=sandbox,
        llm_client=_MockAsyncOpenAI(ok_llm),
    )

    # Stranger without code gets exactly 1 polite prompt within 24h cooldown
    r_s1 = await orch.handle_inbound_message(
        InboundMessage(rowid=1, guid="m1", sender=stranger, text="你好在吗", timestamp_unix=time.time())
    )
    r_s2 = await orch.handle_inbound_message(
        InboundMessage(rowid=2, guid="m2", sender=stranger, text="再发一条", timestamp_unix=time.time())
    )
    assert r_s1 is not None and "邀请内测阶段" in r_s1
    assert r_s2 is None  # Cooldown suppressed spam!

    # Member redeems invite code
    r_act = await orch.handle_inbound_message(
        InboundMessage(rowid=3, guid="m3", sender=member, text=code, timestamp_unix=time.time())
    )
    assert r_act is not None and "激活成功" in r_act
    assert store.is_authorized_user(member) is True

    # Exhausted single-use code rejected for stranger
    ok_exhausted, _ = store.redeem_invite_code(stranger, code)
    assert ok_exhausted is False

    # Activated member chats WITHOUT needing /ai (daily_msg_limit=2)
    r_m1 = await orch.handle_inbound_message(
        InboundMessage(rowid=4, guid="m4", sender=member, text="第一条正式提问", timestamp_unix=time.time())
    )
    r_m2 = await orch.handle_inbound_message(
        InboundMessage(rowid=5, guid="m5", sender=member, text="第二条正式提问", timestamp_unix=time.time())
    )
    r_m3 = await orch.handle_inbound_message(
        InboundMessage(rowid=6, guid="m6", sender=member, text="第三条超额提问", timestamp_unix=time.time())
    )
    assert r_m1 == "内测成员回复成功"
    assert r_m2 == "内测成员回复成功"
    assert r_m3 is not None and "上限" in r_m3

    # Backward-compat legacy INST-* invite code redemption check
    legacy_code = store.create_invite_code(
        code="INST-LEGACY", max_uses=1, daily_msg_limit=5, daily_browser_limit=2, note="legacy"
    )
    assert legacy_code == "INST-LEGACY"
    legacy_member = "+8613700003333"
    r_leg = await orch.handle_inbound_message(
        InboundMessage(
            rowid=10,
            guid="m10",
            sender=legacy_member,
            text="请帮我用 INST-LEGACY 激活",
            timestamp_unix=time.time(),
        )
    )
    assert r_leg is not None and "激活成功" in r_leg
    assert store.is_authorized_user(legacy_member) is True
    store.close()




async def _check_hypertext_formatter_and_attachments(tmp_dir: Path) -> None:
    from .formatter import (
        format_for_imessage,
        is_sensitive_attachment_path,
        render_mobile_html_card,
    )

    raw_md = (
        "### 今日快报\n"
        "**核心结论**：算力成本下降 `35%`，公式 3 * 4 = 12 保持不变。\n\n"
        "| 方案 | 延迟 |\n"
        "| --- | --- |\n"
        "| Flash | 0.4s |\n\n"
        "- 详见 [官方发布页](https://example.com/release?v=4#top)"
    )
    cleaned = format_for_imessage(raw_md)
    assert "**" not in cleaned and "###" not in cleaned and "`" not in cleaned
    assert "| --- |" not in cleaned
    assert "3 * 4 = 12" in cleaned
    assert "· 方案：Flash ｜ 延迟：0.4s" in cleaned
    assert "\nhttps://example.com/release?v=4#top" in cleaned

    card_html = render_mobile_html_card(
        title="安全测试 <script>alert(1)</script>",
        body="正文 <img src=x onerror=alert(1)>\n[参考](https://example.com/doc)",
    )
    assert "<!DOCTYPE html>" in card_html
    assert "<script>" not in card_html
    assert "&lt;script&gt;" in card_html
    assert 'href="https://example.com/doc"' in card_html

    blocked_env, _ = is_sensitive_attachment_path(tmp_dir / ".env")
    blocked_db, _ = is_sensitive_attachment_path(tmp_dir / "state.db")
    safe_card, _ = is_sensitive_attachment_path(tmp_dir / "report.html")
    assert blocked_env is True and blocked_db is True and safe_card is False

    bridge = CliBridge(quiet=True)
    sample_file = tmp_dir / "早报卡片.html"
    sample_file.write_text(card_html, encoding="utf-8")
    await bridge.send_attachment("+8613800138000", sample_file, caption="**请查收**")
    assert len(bridge.attachments) == 1
    assert bridge.outbox[-1][1] == "请查收"


async def _check_hypertext_instant_ack_interruption_and_hot_whitelist(tmp_dir: Path) -> None:
    sub_dir = tmp_dir / "hypertext_ux"
    sub_dir.mkdir(parents=True, exist_ok=True)
    store = StateStore(sub_dir / "state.db")
    bridge = CliBridge(quiet=True)
    gate = HumanGate(default_timeout=3.0)
    admin = "+8613800138000"

    # Hot allow-user / revoke-user check
    vip = "+8613600009999"
    store.allow_user(vip, note="VIP")
    assert store.is_authorized_user(vip) is True
    store.revoke_user(vip)
    assert store.is_authorized_user(vip) is False
    assert store.is_revoked_user(vip) is True

    slow_started = asyncio.Event()

    async def slow_llm(kwargs: dict[str, Any], call_num: int) -> _MockCompletion:
        slow_started.set()
        await asyncio.sleep(2.0)
        return _MockCompletion(
            choices=[_MockChoice(message=_MockMessage(content="**最终结果**"))]
        )

    cfg = AgentConfig(
        provider_mode="deepseek",
        deepseek_api_key="sk-valid-ds-hypertext",
        allowed_senders={admin},
        enable_instant_ack=True,
        fast_ack_delay_sec=0.03,
        heartbeat_interval_sec=10.0,
        state_dir=sub_dir,
    )
    orch = HyperTextOrchestrator(
        config=cfg,
        store=store,
        bridge=bridge,
        human_gate=gate,
        llm_client=_MockAsyncOpenAI(slow_llm),
    )

    bg_turn = asyncio.create_task(
        orch.handle_inbound_message(
            InboundMessage(
                rowid=1,
                guid="x1",
                sender=admin,
                text="帮我查一下今天最新的行业新闻汇总",
                timestamp_unix=time.time(),
            )
        )
    )
    await asyncio.wait_for(slow_started.wait(), timeout=1.0)
    await asyncio.sleep(0.06)
    # Instant Ack must have been sent before slow LLM finishes
    assert any("收到" in m[1] for m in bridge.outbox)

    # Live status query without blocking
    status_reply = await orch.handle_inbound_message(
        InboundMessage(rowid=2, guid="x2", sender=admin, text="在干嘛", timestamp_unix=time.time())
    )
    assert status_reply is not None and "还在办" in status_reply

    # Live stop command cancels running background turn immediately
    stop_reply = await orch.handle_inbound_message(
        InboundMessage(rowid=3, guid="x3", sender=admin, text="停", timestamp_unix=time.time())
    )
    assert stop_reply == "好，刚才那件事已经停下了。有新安排你随时说。"
    cancelled_res = await asyncio.wait_for(bg_turn, timeout=1.0)
    assert cancelled_res is None
    store.close()


async def _run_all_async(tmp_dir: Path) -> list[str]:
    results: list[str] = []
    _check_attributed_body()
    results.append(
        "[PASS] 1/10 attributedBody 解码 (<128B / >128B 0x81 / >65535B 0x82 + U+FFFC 剥离)"
    )

    _check_chat_db_readonly_and_filtering(tmp_dir)
    results.append(
        "[PASS] 2/10 chat.db 只读游标对齐 + Tapback(2000) 过滤 + 发送者白名单拦截"
    )

    _check_fts5_hybrid_memory(tmp_dir)
    results.append(
        "[PASS] 3/10 SQLite FTS5 中英混合检索 ('虹桥 冰美式' / '靠窗' / 'starb' / 更新与删除同步)"
    )

    await _check_deepseek_and_openai_isolation(tmp_dir)
    results.append(
        "[PASS] 4/10 DeepSeek V4.1-Flash 原生思考链 & Server Tools 往返 + OpenAI 隔离与崩溃历史自愈"
    )

    await _check_human_gate_2fa_no_deadlock(tmp_dir)
    results.append(
        "[PASS] 5/10 HumanGate 异步 2FA 挂起与 iMessage 验证码无死锁唤醒"
    )

    await _check_proactive_cron(tmp_dir)
    results.append(
        "[PASS] 6/10 Proactive 定时任务引擎 (direct_notify & agent_wakeup)"
    )

    await _check_same_apple_id_self_chat(tmp_dir)
    results.append(
        "[PASS] 7/10 同 Apple ID 自聊模式 (4层防回环 + 双行镜像去重 + 裸验证码免前缀解锁)"
    )

    await _check_multiuser_invites_quotas_and_browser_isolation(tmp_dir)
    results.append(
        "[PASS] 8/10 多用户内测隔离体系 (SHA-256 独立浏览器沙箱 + 邀请码核销 + 24h防骚扰 + 日配额)"
    )

    await _check_hypertext_formatter_and_attachments(tmp_dir)
    results.append(
        "[PASS] 9/10 HyperText 零Markdown排版引擎 + 独立富媒体链接 + 手机HTML卡片与附件安全门禁"
    )

    await _check_hypertext_instant_ack_interruption_and_hot_whitelist(tmp_dir)
    results.append(
        "[PASS] 10/10 HyperText 双速秒回确认 + 后台不阻塞插话(查进度/随时喊停) + 热更新白名单"
    )
    return results


def run_self_test() -> bool:
    """Execute all 10 subsystem self-tests in an isolated temporary directory."""
    print("============================================================")
    print("🧪 HyperText v0.5.0 — 自检套件 (--self-test)")
    print("============================================================")
    try:
        with tempfile.TemporaryDirectory(prefix="hypertext_selftest_") as tmp:
            lines = asyncio.run(_run_all_async(Path(tmp)))
            for line in lines:
                print(line)
        print("------------------------------------------------------------")
        print("✅ 全部 10 项核心子系统自检通过！")
        return True
    except Exception as exc:
        print(f"❌ 自检失败: {exc}")
        raise
