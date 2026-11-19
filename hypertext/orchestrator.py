"""HyperText Cognitive Orchestrator: Instant Ack, Live Interruption, Hybrid Memory, DeepSeek/OpenAI Routing & Attachments."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import html
import json
import logging
import re
import sqlite3
import time
import urllib.request
from pathlib import Path
from typing import Any

from .bridge_imessage import CliBridge, InboundMessage, MacIMessageBridge, MessageBridge
from .config import AgentConfig, normalize_sender
from .dreamina_client import (
    DreaminaClient,
    extract_dreamina_query,
    is_dreamina_reset_command,
    is_explicit_dreamina_request,
)
from .formatter import (
    format_for_imessage,
    is_sensitive_attachment_path,
    render_mobile_html_card,
    sanitize_attachment_filename,
    split_reply_and_standalone_links,
)
from .human_gate import HumanGate
from .message_routing import (
    APPROVAL_REPLY_HINT, _is_status_command, _is_stop_command, approval_answer, is_explicit_approval_reply,
)
from .harnesses_client import (
    AGYClient,
    CodexClient,
    DSHClient,
    detect_explicit_channels,
    extract_agy_query,
    extract_codex_query,
    extract_dsh_query,
    is_agy_reset_command,
    is_codex_reset_command,
    is_dsh_reset_command,
    is_local_mac_compound_action,
)
from .sandbox_browser import BrowserSandbox
from .store_sqlite import StateStore
from .workbuddy_client import (
    WorkBuddyClient,
    extract_workbuddy_query,
    is_compound_workbuddy_request,
    is_explicit_workbuddy_request,
    is_workbuddy_reset_command,
)
from .xiaona_client import (
    XiaoNaClient,
    extract_xiaona_query,
    format_xiaona_reply,
    is_compound_xiaona_request,
    is_explicit_xiaona_request,
    is_xiaomiguan_alias_used,
    is_xiaona_reset_command,
    strip_delivered_urls_from_text,
)

logger = logging.getLogger("hypertext.orchestrator")

_BROWSER_INTENT_KEYWORDS = (
    "打开",
    "登录",
    "预订",
    "订座",
    "订票",
    "买",
    "网页",
    "浏览器",
    "抓取",
    "值机",
    "open",
    "book",
    "login",
)

_SEARCH_INTENT_KEYWORDS = (
    "查",
    "搜",
    "新闻",
    "天气",
    "调研",
    "对比",
    "看看",
    "最新",
    "总结",
    "股价",
    "行情",
    "search",
    "weather",
    "news",
)

def _normalize_jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, list):
        return [_normalize_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _normalize_jsonable(v) for k, v in obj.items()}
    if hasattr(obj, "model_dump") and callable(obj.model_dump):
        return _normalize_jsonable(obj.model_dump())
    if hasattr(obj, "__dict__"):
        return _normalize_jsonable(vars(obj))
    return str(obj)


def _fetch_url_sync(url: str, max_chars: int = 12000, timeout: float = 15.0) -> str:
    if not url.startswith(("http://", "https://")):
        raise ValueError("仅支持 http:// 或 https:// 协议链接")
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
            )
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(512 * 1024)
        charset = resp.headers.get_content_charset() or "utf-8"
    decoded = raw.decode(charset, errors="replace")
    decoded = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", decoded)
    decoded = re.sub(r"(?s)<[^>]+>", " ", decoded)
    decoded = html.unescape(decoded)
    decoded = re.sub(r"\s+", " ", decoded).strip()
    return decoded[:max_chars]


async def _simple_fetch_url(url: str, max_chars: int = 12000, timeout: float = 15.0) -> str:
    return await asyncio.to_thread(_fetch_url_sync, url, max_chars, timeout)


def _is_heavy_browser_intent(user_text: str) -> bool:
    low = (user_text or "").strip().lower()
    if not low:
        return False
    return any(kw in low for kw in _BROWSER_INTENT_KEYWORDS)


def _summarize_task_label(user_text: str, has_images: bool) -> str:
    clean = re.sub(r"\s+", " ", (user_text or "").strip())
    if not clean or clean == "[用户发送了图片]":
        return "查看你发的图片" if has_images else "刚才交代的事"
    clean = re.sub(r"https?://\S+", "链接内容", clean).strip()
    if len(clean) > 18:
        return clean[:18] + "…"
    return clean


# ---------------------------------------------------------------------------
# 对话与进度播报四原则 (HyperText v0.5.1 Conversational Progress UX Principles)
# 1. 单一出口原则 (Single-Exit): 所有工具（含 BrowserSandbox、搜索、脚本与文档工具）均为安静底层能力
#    (Quiet Tools)；除 2FA/支付确认 (HumanGate) 与交付最终附件外，工具内部严禁擅自向用户发送进度消息。
# 2. 过程不上屏原则 (Zero Plumbing on Screen): 中间 tool_calls 伴随的 assistant.content、步骤号、
#    访问域名与工程术语仅保存在内存态 (turn_ctx) 与日志中，绝不直接发往用户手机。
# 3. 派活单次回执 + 低频单次兜底 (One Ack + Capped Low-Frequency Reassurance): 耗时任务仅在超过
#    fast_ack_delay_sec 时发送 1 次自然接单回执；此后严格以 ack_sent_at 为时间锚点，满
#    heartbeat_interval_sec (默认 60s) 最多发送 max_heartbeat_per_turn (默认 1) 次无术语安抚，随后静默至交付结果。
# 4. 按需内省与阻塞升级 (On-Demand Introspection & Blocking Escalation): 仅当用户主动查进度
#    (「进度」「到哪了」) 时读取内存态简报，或遇到必须人工验证码/拍板时通过 HumanGate 阻塞升级。
# ---------------------------------------------------------------------------


class HyperTextOrchestrator:
    """HyperText conversational orchestrator (Single-Exit Quiet-Tool UX, hybrid memory & attachments)."""

    def __init__(
        self,
        config: AgentConfig,
        store: StateStore,
        bridge: MessageBridge,
        human_gate: HumanGate,
        browser_sandbox: BrowserSandbox | None = None,
        llm_client: Any = None,
        fallback_llm_client: Any = None,
        embedding_client: Any = None,
        xiaona_client: XiaoNaClient | None = None,
        workbuddy_client: WorkBuddyClient | None = None,
        dsh_client: DSHClient | None = None,
        codex_client: CodexClient | None = None,
        agy_client: AGYClient | None = None,
        dreamina_client: DreaminaClient | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.bridge = bridge
        self.human_gate = human_gate
        if hasattr(self.bridge, "attach_human_gate") and callable(
            getattr(self.bridge, "attach_human_gate")
        ):
            self.bridge.attach_human_gate(self.human_gate)

        if self.config.enable_invite_codes and self.config.invite_codes:
            self.store.seed_static_invite_codes(
                self.config.invite_codes,
                daily_msg_limit=self.config.default_member_daily_msg_limit,
                daily_browser_limit=self.config.default_member_daily_browser_limit,
            )

        self.browser_sandbox = browser_sandbox or BrowserSandbox(
            config=config, human_gate=human_gate, bridge=bridge
        )
        self.xiaona_client = xiaona_client or XiaoNaClient(self.config)
        self.workbuddy_client = workbuddy_client or WorkBuddyClient(self.config)
        self.dsh_client = dsh_client or DSHClient(self.config)
        self.codex_client = codex_client or CodexClient(self.config)
        self.agy_client = agy_client or AGYClient(self.config)
        self.dreamina_client = dreamina_client or DreaminaClient(self.config)
        self._injected_llm_client = llm_client
        self._injected_fallback_client = fallback_llm_client
        self._injected_embedding_client = embedding_client
        self._client_cache: dict[tuple[str, str], Any] = {}
        self._db_lock = asyncio.Lock()
        self._active_turns: dict[str, list[dict[str, Any]]] = {}

    @staticmethod
    def _craft_instant_ack(user_text: str, has_images: bool = False) -> str:
        """Craft a warm, natural, zero-jargon human assistant acknowledgment tailored to user intent."""
        txt = (user_text or "").strip()
        low = txt.lower()

        channels = detect_explicit_channels(txt)
        if channels:
            label_map = {
                "xiaona": "小蜜罐" if is_xiaomiguan_alias_used(txt) else "小纳",
                "workbuddy": "WorkBuddy",
                "dsh": "DSH",
                "codex": "Codex",
                "agy": "AGY",
                "dreamina": "即梦画布",
            }
            if len(channels) >= 2:
                joined_names = "、".join(label_map[ch] for ch in channels)
                return f"收到，我调起 {joined_names} 处理一下，好了回你。"
            only_ch = channels[0]
            if only_ch == "xiaona":
                return f"收到，我问一下{label_map['xiaona']}，好了回你。"
            if only_ch == "dreamina":
                return "收到，正在用即梦画布生成，好了直接发你。"
            return f"收到，我交给 {label_map[only_ch]} 处理一下，好了回你。"

        if _is_heavy_browser_intent(txt):
            return "好嘞，收到。我这就打开页面去办，中途如果要验证码或拍板我发消息喊你。"

        if "http://" in low or "https://" in low:
            return "收到，我先点开这个链接看一眼，理清楚马上回你。"

        if has_images:
            return "收到图了，我看一下哈。"

        if any(kw in low for kw in _SEARCH_INTENT_KEYWORDS):
            return "收到，我去查一下最新情况，马上回你。"

        if len(txt) > 10:
            return "收到，我处理一下马上回你。"

        return ""

    def _is_admin_sender(self, sender: str) -> bool:
        norm = normalize_sender(sender)
        if not norm:
            return False
        if self.store.is_revoked_user(norm):
            return False
        if norm == "local-cli-user" and isinstance(self.bridge, CliBridge):
            return True
        return norm in self.config.allowed_senders

    def _get_or_create_client(self, api_key: str, base_url: str, is_fallback: bool = False) -> Any:
        if not is_fallback and self._injected_llm_client is not None:
            return self._injected_llm_client
        if is_fallback and self._injected_fallback_client is not None:
            return self._injected_fallback_client
        if self._injected_llm_client is not None and self._injected_fallback_client is None:
            return self._injected_llm_client

        cache_key = (api_key, base_url)
        if cache_key in self._client_cache:
            return self._client_cache[cache_key]

        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._client_cache[cache_key] = client
        return client

    async def _compute_optional_embedding(self, text: str) -> list[float] | None:
        if not text or not text.strip():
            return None
        emb_key = getattr(self.config, "embedding_api_key", "") or self.config.openai_api_key
        emb_url = getattr(self.config, "embedding_base_url", "") or self.config.openai_base_url
        emb_model = getattr(self.config, "embedding_model", "")
        if self._injected_embedding_client is None and not (emb_model and emb_key):
            return None

        emb_client = self._injected_embedding_client
        if emb_client is None:
            emb_client = self._get_or_create_client(
                emb_key,
                emb_url,
                is_fallback=True,
            )

        try:
            resp = await emb_client.embeddings.create(
                model=self.config.embedding_model,
                input=text.strip()[:4000],
            )
            vec = resp.data[0].embedding
            return [float(x) for x in vec]
        except Exception:
            logger.debug("Optional embedding computation skipped/failed", exc_info=True)
            return None

    def _build_tools(
        self,
        is_deepseek_native: bool,
        include_xiaona: bool = False,
        include_workbuddy: bool = False,
        include_dsh: bool = False,
        include_codex: bool = False,
        include_agy: bool = False,
        include_dreamina: bool = False,
    ) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = [
            {
                "type": "function",
                "function": {
                    "name": "save_memory",
                    "description": "保存或更新用户的长期个人偏好、重要事实、常用地址/账号别名等记忆。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "key": {
                                "type": "string",
                                "description": "简短唯一的记忆标识，如 coffee_pref 或 home_station",
                            },
                            "value": {
                                "type": "string",
                                "description": "具体的事实或偏好描述",
                            },
                            "tags": {
                                "type": "string",
                                "description": "可选标签（逗号分隔），便于后续检索",
                            },
                        },
                        "required": ["key", "value"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_memory",
                    "description": "通过中英关键词检索当前用户的长期记忆事实。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "搜索词（支持中文词组、人名、地点或英文前缀）",
                            },
                            "limit": {
                                "type": "integer",
                                "description": "最多返回条数，默认 10",
                            },
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "delete_memory",
                    "description": "根据 key 删除过时或用户要求遗忘的长期记忆。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "key": {
                                "type": "string",
                                "description": "要删除的记忆 key",
                            },
                        },
                        "required": ["key"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "create_and_send_document",
                    "description": (
                        "当内容较长（如深度调研报告、长清单、多维对比方案、结构化数据表）时，"
                        "将其生成精美的手机端 HTML 阅读卡片（或 txt/md/csv 文件）并通过 iMessage 附件直接发给用户，"
                        "随后气泡正文只需回复 3 句核心结论。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "文档或卡片标题，如 '今日AI行业早报' 或 '周末东京航班对比'",
                            },
                            "content": {
                                "type": "string",
                                "description": "完整文档内容。当 doc_format='html' 时可编写完整自定义 <!DOCTYPE html>+<style> 网页或语义化 HTML 片段（<h2>/<p>/<table> 等）；当 doc_format='pdf' 时传入结构化 HTML 片段或段落表格，系统自动排版并生成 iPhone 内联预览 PDF。",
                            },
                            "doc_format": {
                                "type": "string",
                                "enum": ["html", "pdf", "txt", "md", "csv"],
                                "description": "附件格式：用户要求攻略/行程单/报告优先选 'pdf'（iPhone 气泡内直接预览）；用户要求做网页/HTML 选 'html'（鼓励针对主题设计精美自定义 HTML+CSS）",
                            },
                            "filename": {
                                "type": "string",
                                "description": "可选自定义文件名，如 '北京周末攻略_1115.pdf' 或 'AI早报_1115.html'",
                            },
                            "caption": {
                                "type": "string",
                                "description": "可选随附件发送的一句话简短说明",
                            },
                        },
                        "required": ["title", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "send_local_attachment",
                    "description": "将本地已生成好的安全文件（如截图、导出表格、文档）作为 iMessage 附件发送给用户。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {
                                "type": "string",
                                "description": "本地文件完整路径",
                            },
                            "caption": {
                                "type": "string",
                                "description": "可选随附件附带的一句话说明",
                            },
                        },
                        "required": ["file_path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "run_browser_task",
                    "description": "仅用于必须真实浏览器交互的操作（账号登录、填写提交表单、购票选座结算、受限重交互动态 SPA、或应用户要求截取实时网页截图）。【严禁滥用】凡属常规信息查询、旅游攻略/去哪玩/行程规划、天气、新闻、餐厅景点查询或公开网页阅读，严禁调用本工具，必须优先使用内置联网搜索与网页读取（快 5～10 倍）！",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task_prompt": {
                                "type": "string",
                                "description": "给浏览器代理的详细操作指令与目标",
                            },
                            "max_steps": {
                                "type": "integer",
                                "description": "最大操作步数，默认 25",
                            },
                        },
                        "required": ["task_prompt"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ask_human_confirmation",
                    "description": "涉及付款、下单、删除、退订或需要验证码时，通过 iMessage 实时向用户提问并等待回复。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "简明扼要地向用户说明需要确认的事项或需要的验证码",
                            },
                        },
                        "required": ["question"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "schedule_task",
                    "description": "创建一次性提醒（direct_notify）或后台主动唤醒巡检任务（agent_wakeup）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task_type": {
                                "type": "string",
                                "enum": ["direct_notify", "agent_wakeup"],
                                "description": "direct_notify 直接发提醒文案；agent_wakeup 到点唤醒助理执行任务再汇报",
                            },
                            "payload": {
                                "type": "string",
                                "description": "提醒文本或唤醒后要执行的指令描述",
                            },
                            "delay_seconds": {
                                "type": "number",
                                "description": "距离首次触发的秒数",
                            },
                            "interval_seconds": {
                                "type": "number",
                                "description": "周期重复秒数（0 表示仅触发一次）",
                            },
                        },
                        "required": ["task_type", "payload", "delay_seconds"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_scheduled_tasks",
                    "description": "列出当前用户名下所有启用中的定时任务。",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "cancel_scheduled_task",
                    "description": "取消指定 ID 的定时任务。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task_id": {
                                "type": "integer",
                                "description": "定时任务 ID",
                            },
                        },
                        "required": ["task_id"],
                    },
                },
            },
        ]

        if include_xiaona:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "ask_xiaona",
                        "description": (
                            "向主理人私人工作台助理「小纳 / 小蜜罐」发起咨询或查询（仅在主理人明确点名时可用），"
                            "返回小纳的回复与媒体链接。"
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "message": {
                                    "type": "string",
                                    "description": "发给小纳的具体问题或指令",
                                },
                            },
                            "required": ["message"],
                        },
                    },
                }
            )

        if include_workbuddy:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "ask_workbuddy",
                        "description": (
                            "调用主理人本机 WorkBuddy（codebuddy）智能工作台执行代码生成、HTML/PPT/文档制作或分析任务"
                            "（仅在主理人明确点名 WorkBuddy / /wb 时可用）。"
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "message": {
                                    "type": "string",
                                    "description": "交给 WorkBuddy 执行的完整指令与上下文",
                                },
                            },
                            "required": ["message"],
                        },
                    },
                }
            )

        if include_dsh:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "ask_dsh",
                        "description": (
                            "调用主理人本机 DeepSeek Harness（DSH）智能工作台执行深度推理、代码架构分析或文档生成"
                            "（仅在主理人明确点名 DSH / /dsh 时可用）。"
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "message": {
                                    "type": "string",
                                    "description": "交给 DSH 执行的完整指令与上下文",
                                },
                            },
                            "required": ["message"],
                        },
                    },
                }
            )

        if include_codex:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "ask_codex",
                        "description": (
                            "调用主理人本机 OpenAI Codex CLI 智能工作台执行工程重构、代码审查、脚本编写或方案评估"
                            "（仅在主理人明确点名 Codex / /codex / /cdx 时可用）。"
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "message": {
                                    "type": "string",
                                    "description": "交给 Codex 执行的完整指令与上下文",
                                },
                            },
                            "required": ["message"],
                        },
                    },
                }
            )

        if include_agy:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "ask_agy",
                        "description": (
                            "调用主理人本机 Google Antigravity（AGY）智能工作台执行架构设计、前端交互或跨视角方案对比"
                            "（仅在主理人明确点名 AGY / Antigravity / /agy 时可用）。"
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "message": {
                                    "type": "string",
                                    "description": "交给 AGY 执行的完整指令与上下文",
                                },
                            },
                            "required": ["message"],
                        },
                    },
                }
            )

        if include_dreamina:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "run_dreamina_canvas",
                        "description": (
                            "调用主理人本机即梦画布（dreamina-canvas）生成高清图片、视频短片、纯音乐或人声配音，"
                            "并自动将产物与即梦画布链接发回手机（仅在主理人明确点名即梦 / /即梦 / /dreamina / /jm 时可用）。"
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "prompt": {
                                    "type": "string",
                                    "description": "完整的画面、视频镜头、音乐风格或配音台词描述",
                                },
                                "media_type": {
                                    "type": "string",
                                    "enum": ["image", "video", "audio", "music", "tts"],
                                    "description": "可选生成类型：image（图片）、video（视频）、music（纯音乐）、tts（配音）",
                                },
                                "ratio": {
                                    "type": "string",
                                    "description": "可选画幅比例，如 1:1、3:4、16:9、9:16、21:9",
                                },
                                "duration_sec": {
                                    "type": "number",
                                    "description": "可选时长（秒）：视频支持 4~10 秒，音乐支持 30~180 秒",
                                },
                            },
                            "required": ["prompt"],
                        },
                    },
                }
            )

        if is_deepseek_native:
            if self.config.enable_deepseek_web_tools:
                tools.append({"type": "deepseek_web_search"})
                tools.append({"type": "deepseek_read_url"})
        else:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "fetch_url_text",
                        "description": "抓取指定 http/https 网页的正文文本内容。",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "url": {
                                    "type": "string",
                                    "description": "完整的 http:// 或 https:// 网页地址",
                                },
                            },
                            "required": ["url"],
                        },
                    },
                }
            )

        return tools

    async def _build_system_prompt(
        self,
        sender: str,
        query_hint: str,
        include_xiaona: bool = False,
        include_workbuddy: bool = False,
        include_dsh: bool = False,
        include_codex: bool = False,
        include_agy: bool = False,
        include_dreamina: bool = False,
    ) -> str:
        now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")
        q_emb = await self._compute_optional_embedding(query_hint)
        async with self._db_lock:
            recalled = self.store.search_memories(
                sender=sender,
                query=query_hint,
                limit=8,
                query_embedding=q_emb,
            )
            if not recalled:
                recalled = self.store.search_memories(sender=sender, query="", limit=5)

        mem_lines = [
            f"- [{m['key']}] {m['value']}" + (f" (tags: {m['tags']})" if m.get("tags") else "")
            for m in recalled
        ]
        mem_block = "\n".join(mem_lines) if mem_lines else "（暂无已存长期记忆）"
        enabled_tools: list[str] = []
        if include_xiaona:
            enabled_tools.append("`ask_xiaona`（小纳/小蜜罐）")
        if include_workbuddy:
            enabled_tools.append("`ask_workbuddy`（WorkBuddy）")
        if include_dsh:
            enabled_tools.append("`ask_dsh`（DSH）")
        if include_codex:
            enabled_tools.append("`ask_codex`（Codex）")
        if include_agy:
            enabled_tools.append("`ask_agy`（AGY）")
        if include_dreamina:
            enabled_tools.append("`run_dreamina_canvas`（即梦画布多媒体生成）")

        if len(enabled_tools) >= 2:
            xiaona_note = (
                f"\n7. 多智能体协同竞技场（本轮已显式授权 {'、'.join(enabled_tools)}）："
                "请逐一调用用户点名的每个专属智能体工具获取独立方案，再提炼各方核心异同与最佳建议汇报给用户。\n"
            )
        elif len(enabled_tools) == 1:
            xiaona_note = (
                f"\n7. 专属工作台联动（本轮已显式授权 {enabled_tools[0]}）："
                "请先调用该专属工具获取结果，再结合 `schedule_task` 等本地工具一次性办妥汇报。\n"
            )
        else:
            xiaona_note = "\n"

        return (
            f"你是用户的贴身私人助理（由 HyperText 驱动，当前时间：{now_str}），通过手机 iMessage 与用户一对一沟通。\n\n"
            "【贴身助理交互铁律（必须严格遵守）】\n"
            "1. 真人贴身助理语气：自然、干脆、温暖、靠谱，像真人助理发短信一样说话，绝不端腔作势、不说机械套话。\n"
            "2. 沟通四原则：「结论先行，只留影响判断的部分，过程不写，重复合并」。严禁输出任何 Markdown 格式符号"
            "（严禁出现 **加粗**、### 标题、代码块 ```、Markdown 表格 |---|、[标题](链接)），"
            "也严禁向用户提 SQLite、FTS5、tool、headless、API、沙盒、函数调用 等任何底层技术黑话。\n"
            "3. 一屏短对话 + 重内容走附件（PDF/HTML）：每次气泡正文控制在手机一屏内（通常 3～8 行短句，适当空行）。"
            "如果用户要旅游攻略、行程单、详细报告或多维对比方案，优先调用 `create_and_send_document(doc_format=\"pdf\")` 生成 iPhone 可直接预览的排版文档"
            "（若用户明确要求网页/HTML，则用 `doc_format=\"html\"` 并可编写精美的自定义 HTML+CSS），气泡里只写 3 句核心结论！\n"
            "4. 链接必带且独立成行：凡涉及新闻、网页、店铺、车次航班或参考来源，必须在气泡末尾每条链接单独占一行放纯正的 "
            "https://... 地址（前后不要加括号或文字粘连），系统会自动拆分为独立原生预览卡片发送。\n"
            "5. 检索与浏览器严格分流（严禁慢速滥用）：凡属查资料、旅游攻略/去哪玩、天气、新闻、餐厅景点评价或公开网页阅读，"
            "必须直接用内置联网搜索与网页读取（快 5～10 倍），严禁调用 `run_browser_task`！只有遇到必须登录账号、填表提交、预订结算、"
            "搜索无法获取的受限动态页面、或用户明确要求网页截图时，才调用 `run_browser_task`。\n"
            "6. 记忆与安全底线：用户的个人偏好、习惯与重要事实主动用 `save_memory` 记录，需回忆时用 `search_memory`；"
            "涉及付款、下单、删除、退订或对外发信等不可逆动作，必须先通过 `ask_human_confirmation` 向用户确认拍板，严禁编造实时数据。"
            f"{xiaona_note}\n"
            f"【与当前对话相关的用户长期记忆】\n{mem_block}"
        )

    def _detect_pdf_chrome_binary(self) -> str | None:
        """Detect a local Chromium/Chrome binary for headless print-to-PDF export."""
        if self.config.chromium_executable_path:
            cand = Path(self.config.chromium_executable_path).expanduser()
            if cand.exists():
                return str(cand)
        detect_fn = getattr(self.browser_sandbox, "_detect_local_chromium", None)
        if callable(detect_fn):
            detected = detect_fn()
            if isinstance(detected, str) and detected and Path(detected).exists():
                return detected
        for p in (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ):
            if Path(p).exists():
                return p
        return None

    @staticmethod
    async def _render_html_to_pdf(
        chrome_bin: str,
        html_path: Path,
        pdf_path: Path,
        timeout_sec: float = 20.0,
    ) -> bool:
        """Run headless Chrome print-to-pdf with 20s timeout; return True if PDF succeeded."""
        try:
            proc = await asyncio.create_subprocess_exec(
                chrome_bin,
                "--headless=new",
                "--disable-gpu",
                "--no-pdf-header-footer",
                f"--print-to-pdf={pdf_path}",
                html_path.resolve().as_uri(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
            except asyncio.TimeoutError:
                proc.kill()
                with contextlib.suppress(Exception):
                    await proc.communicate()
                return False
            return proc.returncode == 0 and pdf_path.exists() and pdf_path.stat().st_size > 0
        except Exception:
            logger.warning("Headless Chrome print-to-pdf failed; falling back to HTML", exc_info=True)
            return False

    def _resolve_session_and_reply(self, msg: InboundMessage) -> tuple[str, str, bool]:
        """Resolve (canonical_session_sender, outbound_reply_recipient, is_admin)."""
        raw_norm = normalize_sender(msg.sender)
        is_admin = self._is_admin_sender(raw_norm)
        if self.config.allow_self_chat and is_admin:
            session_sender = self.config.canonical_admin_sender or raw_norm or msg.sender
            reply_recipient = (
                self.config.self_chat_reply_to.strip()
                or (msg.reply_target or "").strip()
                or msg.sender
            )
            return session_sender, reply_recipient, True
        reply_recipient = (msg.reply_target or "").strip() or msg.sender
        return msg.sender, reply_recipient, is_admin

    async def _deliver_formatted_reply(self, recipient: str, formatted_reply: str) -> None:
        """Send formatted reply, splitting trailing/standalone URLs into pure URL bubbles when enabled."""
        if not formatted_reply:
            return
        if self.config.split_standalone_links:
            main_text, standalone_urls = split_reply_and_standalone_links(
                formatted_reply, max_links=3
            )
            if standalone_urls:
                is_real_mac = isinstance(self.bridge, MacIMessageBridge)
                if main_text:
                    await self.bridge.send_text(recipient, main_text)
                    if is_real_mac:
                        await asyncio.sleep(0.2)
                for idx, url in enumerate(standalone_urls):
                    await self.bridge.send_text(recipient, url)
                    if is_real_mac and idx + 1 < len(standalone_urls):
                        await asyncio.sleep(0.2)
                return
        await self.bridge.send_text(recipient, formatted_reply)

    async def _execute_local_tool(
        self,
        sender: str,
        fn_name: str,
        raw_args: str,
        turn_ctx: dict[str, Any] | None = None,
    ) -> str:
        try:
            args = json.loads(raw_args) if raw_args and raw_args.strip() else {}
            if not isinstance(args, dict):
                args = {}
        except json.JSONDecodeError as exc:
            return json.dumps({"ok": False, "error": f"Invalid tool JSON arguments: {exc}"}, ensure_ascii=False)

        reply_recipient = (
            str(turn_ctx.get("reply_recipient", "")).strip()
            if turn_ctx is not None
            else ""
        ) or sender

        try:
            if fn_name == "save_memory":
                key = str(args.get("key", "")).strip()
                value = str(args.get("value", "")).strip()
                tags = str(args.get("tags", "")).strip()
                if not key or not value:
                    return json.dumps({"ok": False, "error": "key 和 value 不能为空"}, ensure_ascii=False)
                emb = await self._compute_optional_embedding(f"{key}: {value} {tags}")
                async with self._db_lock:
                    self.store.save_memory(sender, key, value, tags=tags, embedding=emb)
                return json.dumps({"ok": True, "saved_key": key}, ensure_ascii=False)

            if fn_name == "search_memory":
                query = str(args.get("query", "")).strip()
                limit = int(args.get("limit", 10) or 10)
                q_emb = await self._compute_optional_embedding(query) if query else None
                async with self._db_lock:
                    hits = self.store.search_memories(
                        sender, query=query, limit=limit, query_embedding=q_emb
                    )
                return json.dumps({"ok": True, "count": len(hits), "memories": hits}, ensure_ascii=False)

            if fn_name == "delete_memory":
                key = str(args.get("key", "")).strip()
                if not key:
                    return json.dumps({"ok": False, "error": "key 不能为空"}, ensure_ascii=False)
                async with self._db_lock:
                    deleted = self.store.delete_memory(sender, key)
                return json.dumps({"ok": True, "deleted": deleted, "key": key}, ensure_ascii=False)

            if fn_name == "create_and_send_document":
                if turn_ctx is not None:
                    turn_ctx["current_stage"] = "整理结论并生成排版附件"
                    elapsed_so_far = time.time() - float(turn_ctx.get("started_at", time.time()))
                    if (
                        elapsed_so_far >= 4.0
                        and not turn_ctx.get("ack_sent")
                        and not turn_ctx.get("pre_doc_notice_sent")
                    ):
                        turn_ctx["pre_doc_notice_sent"] = True
                        turn_ctx["ack_sent"] = True
                        turn_ctx["ack_sent_at"] = time.time()
                        transition_msg = "资料已经查齐核对好了，正在给你生成排版好的详细附件，几秒钟就发过来。"
                        await self.bridge.send_text(reply_recipient, transition_msg)
                        turn_ctx["ack_sent_at"] = time.time()

                title = str(args.get("title", "")).strip() or "整理文档"
                content = str(args.get("content", ""))
                doc_format = str(args.get("doc_format", "html") or "html").strip().lower()
                if doc_format not in ("html", "pdf", "txt", "md", "csv"):
                    doc_format = "html"
                raw_filename = str(args.get("filename", "")).strip()
                caption = str(args.get("caption", "")).strip()

                ts_tag = dt.datetime.now().strftime("%m%d_%H%M%S")
                stem = raw_filename if raw_filename else f"{title}_{ts_tag}"
                deliverables_dir = self.config.state_dir / "deliverables"
                deliverables_dir.mkdir(parents=True, exist_ok=True)
                actual_format = doc_format

                if doc_format == "pdf":
                    rendered_html = render_mobile_html_card(title=title, body=content)
                    html_name = sanitize_attachment_filename(
                        stem, default_stem=f"HyperText文档_{ts_tag}", ext="html"
                    )
                    pdf_name = sanitize_attachment_filename(
                        stem, default_stem=f"HyperText文档_{ts_tag}", ext="pdf"
                    )
                    html_path = deliverables_dir / html_name
                    pdf_path = deliverables_dir / pdf_name
                    html_path.write_text(rendered_html, encoding="utf-8")

                    chrome_bin = self._detect_pdf_chrome_binary()
                    pdf_ok = False
                    if chrome_bin:
                        pdf_ok = await self._render_html_to_pdf(
                            chrome_bin, html_path, pdf_path, timeout_sec=20.0
                        )
                    if pdf_ok:
                        out_path = pdf_path
                        if html_path != pdf_path:
                            with contextlib.suppress(OSError):
                                html_path.unlink()
                    else:
                        out_path = html_path
                        actual_format = "html"
                else:
                    safe_name = sanitize_attachment_filename(
                        stem, default_stem=f"HyperText文档_{ts_tag}", ext=doc_format
                    )
                    out_path = deliverables_dir / safe_name
                    if doc_format == "html":
                        rendered = render_mobile_html_card(title=title, body=content)
                        out_path.write_text(rendered, encoding="utf-8")
                    elif doc_format == "txt":
                        out_path.write_text(
                            format_for_imessage(f"{title}\n\n{content}"), encoding="utf-8"
                        )
                    elif doc_format == "csv":
                        bom_prefix = "" if content.startswith("\ufeff") else "\ufeff"
                        out_path.write_text(bom_prefix + content, encoding="utf-8")
                    else:
                        out_path.write_text(content, encoding="utf-8")

                await self.bridge.send_attachment(reply_recipient, out_path, caption=caption)
                return json.dumps(
                    {
                        "ok": True,
                        "filename": out_path.name,
                        "file_path": str(out_path),
                        "doc_format": actual_format,
                    },
                    ensure_ascii=False,
                )

            if fn_name == "send_local_attachment":
                raw_fp = str(args.get("file_path", "")).strip()
                caption = str(args.get("caption", "")).strip()
                if not raw_fp:
                    return json.dumps({"ok": False, "error": "file_path 不能为空"}, ensure_ascii=False)
                target_file = Path(raw_fp).expanduser()
                blocked, reason = is_sensitive_attachment_path(target_file)
                if blocked:
                    return json.dumps(
                        {"ok": False, "error": f"安全策略拦截：{reason}"},
                        ensure_ascii=False,
                    )
                if not target_file.exists() or not target_file.is_file():
                    return json.dumps(
                        {"ok": False, "error": f"文件不存在：{target_file.name}"},
                        ensure_ascii=False,
                    )
                await self.bridge.send_attachment(reply_recipient, target_file, caption=caption)
                return json.dumps(
                    {"ok": True, "sent_file": target_file.name},
                    ensure_ascii=False,
                )

            if fn_name == "run_browser_task":
                if turn_ctx is not None:
                    turn_ctx["current_stage"] = "正在页面上核对信息"
                is_admin = self._is_admin_sender(sender)
                async with self._db_lock:
                    allowed_bq, bq_reason = self.store.check_and_increment_quota(
                        sender, kind="browser", is_admin=is_admin
                    )
                if not allowed_bq:
                    return json.dumps({"ok": False, "error": bq_reason}, ensure_ascii=False)

                local_browser_ack_task: asyncio.Task[None] | None = None
                if (
                    self.config.enable_instant_ack
                    and turn_ctx is not None
                    and not turn_ctx.get("ack_sent")
                    and not turn_ctx.get("has_ack_timer")
                ):
                    b_delay = (
                        0.0
                        if self.config.fast_ack_delay_sec <= 0
                        else min(0.4, self.config.fast_ack_delay_sec)
                    )

                    async def _delayed_browser_notice() -> None:
                        if b_delay > 0:
                            await asyncio.sleep(b_delay)
                        if turn_ctx is not None and not turn_ctx.get("ack_sent"):
                            turn_ctx["ack_sent"] = True
                            turn_ctx["ack_sent_at"] = time.time()
                            await self.bridge.send_text(
                                reply_recipient,
                                "好嘞，我这就打开页面去办，中途如果要验证码或拍板我发消息喊你。",
                            )
                            turn_ctx["ack_sent_at"] = time.time()

                    local_browser_ack_task = asyncio.create_task(_delayed_browser_notice())

                prompt = str(args.get("task_prompt", "")).strip()
                max_steps = args.get("max_steps")

                try:
                    try:
                        result = await self.browser_sandbox.run_task(
                            sender=sender,
                            task_prompt=prompt,
                            max_steps=int(max_steps) if max_steps else None,
                            reply_to=reply_recipient,
                            turn_ctx=turn_ctx,
                        )
                    except TypeError:
                        result = await self.browser_sandbox.run_task(
                            sender=sender,
                            task_prompt=prompt,
                            max_steps=int(max_steps) if max_steps else None,
                        )
                finally:
                    if local_browser_ack_task is not None and not local_browser_ack_task.done():
                        local_browser_ack_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await local_browser_ack_task
                return json.dumps({"ok": True, "result": result}, ensure_ascii=False)

            if fn_name == "ask_human_confirmation":
                question = str(args.get("question", "")).strip() or "这件事需要你拍板确认，要继续吗？"
                clean_q = format_for_imessage(question)
                answer = await self.human_gate.request_input(
                    sender=sender,
                    question=f"🔐 [需要你确认] {clean_q}\n{APPROVAL_REPLY_HINT}",
                    bridge=self.bridge,
                    reply_to=reply_recipient,
                )
                return json.dumps({"ok": True, "user_reply": answer}, ensure_ascii=False)

            if fn_name == "schedule_task":
                task_type = str(args.get("task_type", "direct_notify")).strip()
                payload = str(args.get("payload", "")).strip()
                delay_sec = max(0.0, float(args.get("delay_seconds", 60.0)))
                interval_sec = max(0.0, float(args.get("interval_seconds", 0.0) or 0.0))
                run_at = time.time() + delay_sec
                async with self._db_lock:
                    tid = self.store.add_scheduled_task(
                        sender=sender,
                        task_type=task_type,
                        payload=payload,
                        run_at=run_at,
                        interval_seconds=interval_sec,
                    )
                return json.dumps(
                    {
                        "ok": True,
                        "task_id": tid,
                        "task_type": task_type,
                        "run_at": run_at,
                        "interval_seconds": interval_sec,
                    },
                    ensure_ascii=False,
                )

            if fn_name == "list_scheduled_tasks":
                async with self._db_lock:
                    tasks = self.store.list_scheduled_tasks(sender=sender, only_enabled=True)
                return json.dumps({"ok": True, "tasks": tasks}, ensure_ascii=False)

            if fn_name == "cancel_scheduled_task":
                tid = int(args.get("task_id", 0))
                async with self._db_lock:
                    cancelled = self.store.cancel_scheduled_task(tid, sender=sender)
                return json.dumps({"ok": True, "cancelled": cancelled, "task_id": tid}, ensure_ascii=False)

            if fn_name == "ask_xiaona":
                if not self._is_admin_sender(sender):
                    return json.dumps(
                        {
                            "ok": False,
                            "error": "小纳连接的是主理人私人工作台，仅限主理人本人调用。",
                        },
                        ensure_ascii=False,
                    )
                url_cfg, key_cfg, _ = self.xiaona_client.resolve_credentials()
                if not url_cfg or not key_cfg:
                    env_hint = str(
                        self.config.loaded_env_file
                        or (Path(__file__).resolve().parent.parent / ".env")
                    )
                    return json.dumps(
                        {
                            "ok": False,
                            "error": (
                                f"小纳通道已接入，还差一步配置密钥：把 XIAONA_RELAY_KEY=xn_... 写入 {env_hint}"
                                "（或存入钥匙串 XIAONA_RELAY_KEY）即可免重启直接调用。"
                            ),
                        },
                        ensure_ascii=False,
                    )
                q_msg = str(args.get("message", "")).strip()
                if not q_msg:
                    return json.dumps({"ok": False, "error": "message 不能为空"}, ensure_ascii=False)
                if turn_ctx is not None:
                    turn_ctx["current_stage"] = "等待小纳工作台回复"
                res_xn = await self.xiaona_client.ask(
                    q_msg, sender=sender, store=self.store
                )
                delivered_urls: list[str] = []
                delivered_files: list[str] = []
                fallback_urls: list[str] = []
                for media_u in res_xn.media_urls:
                    local_media = await self.xiaona_client.download_media_url(media_u)
                    if local_media is not None:
                        sent_ok = False
                        try:
                            att_ret = await self.bridge.send_attachment(
                                reply_recipient, local_media
                            )
                            sent_ok = att_ret is not False
                        except Exception:
                            logger.warning(
                                "Failed sending ask_xiaona media attachment %s; falling back to URL",
                                local_media.name,
                                exc_info=True,
                            )
                            sent_ok = False
                        if sent_ok:
                            delivered_urls.append(media_u)
                            delivered_files.append(local_media.name)
                            continue
                    fallback_urls.append(media_u)

                clean_xn_reply = strip_delivered_urls_from_text(res_xn.reply, delivered_urls)
                fmt_xn_reply = format_xiaona_reply(clean_xn_reply, fallback_urls)
                return json.dumps(
                    {
                        "ok": res_xn.ok,
                        "reply": clean_xn_reply,
                        "delivered_attachments": delivered_files,
                        "media_urls": fallback_urls,
                        "formatted_reply": fmt_xn_reply,
                        "session_id": res_xn.session_id,
                        "error": res_xn.error,
                    },
                    ensure_ascii=False,
                )

            if fn_name == "ask_workbuddy":
                if not self._is_admin_sender(sender):
                    return json.dumps(
                        {
                            "ok": False,
                            "error": "WorkBuddy 连接的是主理人本机工作台，仅限主理人本人调用；其他问题我可以直接帮你处理。",
                        },
                        ensure_ascii=False,
                    )
                wb_msg = str(args.get("message", "")).strip()
                if not wb_msg:
                    return json.dumps(
                        {"ok": False, "error": "message 不能为空"}, ensure_ascii=False
                    )
                if turn_ctx is not None:
                    turn_ctx["current_stage"] = "等待 WorkBuddy 本机工作台处理"
                res_wb = await self.workbuddy_client.ask(
                    wb_msg, sender=sender, store=self.store
                )
                wb_delivered_files: list[str] = []
                for d_path in res_wb.deliverable_paths:
                    try:
                        att_ret = await self.bridge.send_attachment(reply_recipient, d_path)
                        if att_ret is not False:
                            wb_delivered_files.append(d_path.name)
                    except Exception:
                        logger.warning(
                            "Failed sending ask_workbuddy deliverable %s",
                            d_path.name,
                            exc_info=True,
                        )
                return json.dumps(
                    {
                        "ok": res_wb.ok,
                        "reply": res_wb.reply,
                        "formatted_reply": res_wb.formatted_reply,
                        "delivered_attachments": wb_delivered_files,
                        "session_id": res_wb.session_id,
                        "error": res_wb.error,
                    },
                    ensure_ascii=False,
                )

            if fn_name in ("ask_dsh", "ask_codex", "ask_agy"):
                harness_meta = {
                    "ask_dsh": ("DSH", self.dsh_client),
                    "ask_codex": ("Codex", self.codex_client),
                    "ask_agy": ("AGY", self.agy_client),
                }
                h_label, h_client = harness_meta[fn_name]
                if not self._is_admin_sender(sender):
                    return json.dumps(
                        {
                            "ok": False,
                            "error": f"{h_label} 连接的是主理人本机工作台，仅限主理人本人调用；其他问题我可以直接帮你处理。",
                        },
                        ensure_ascii=False,
                    )
                h_msg = str(args.get("message", "")).strip()
                if not h_msg:
                    return json.dumps(
                        {"ok": False, "error": "message 不能为空"}, ensure_ascii=False
                    )
                if turn_ctx is not None:
                    turn_ctx["current_stage"] = f"等待 {h_label} 本机工作台处理"
                res_h = await h_client.ask(h_msg, sender=sender, store=self.store)
                h_delivered_files: list[str] = []
                for d_path in res_h.deliverable_paths:
                    try:
                        att_ret = await self.bridge.send_attachment(reply_recipient, d_path)
                        if att_ret is not False:
                            h_delivered_files.append(d_path.name)
                    except Exception:
                        logger.warning(
                            "Failed sending %s deliverable %s",
                            fn_name,
                            d_path.name,
                            exc_info=True,
                        )
                return json.dumps(
                    {
                        "ok": res_h.ok,
                        "harness": res_h.harness,
                        "reply": res_h.reply,
                        "formatted_reply": res_h.formatted_reply,
                        "delivered_attachments": h_delivered_files,
                        "session_id": res_h.session_id,
                        "model_used": res_h.model_used,
                        "error": res_h.error,
                    },
                    ensure_ascii=False,
                )

            if fn_name == "run_dreamina_canvas":
                if not self._is_admin_sender(sender):
                    return json.dumps(
                        {
                            "ok": False,
                            "error": "即梦画布连接的是主理人本机账号与积分池，仅限主理人本人调用；其他问题我可以直接帮你处理。",
                        },
                        ensure_ascii=False,
                    )
                dm_prompt = str(args.get("prompt", "")).strip()
                dm_media = str(args.get("media_type", "")).strip()
                dm_ratio = str(args.get("ratio", "")).strip()
                raw_dur = args.get("duration_sec")
                dm_dur: float | None = None
                if raw_dur is not None and str(raw_dur).strip() != "":
                    with contextlib.suppress(ValueError, TypeError):
                        dm_dur = float(raw_dur)
                att_paths = (
                    list(turn_ctx.get("attachment_paths") or [])
                    if turn_ctx is not None
                    else None
                )
                if not dm_prompt and not att_paths:
                    return json.dumps(
                        {"ok": False, "error": "prompt 不能为空"}, ensure_ascii=False
                    )
                if turn_ctx is not None:
                    turn_ctx["current_stage"] = "正在用即梦画布生成多媒体产物"
                res_dm = await self.dreamina_client.ask(
                    dm_prompt,
                    sender=sender,
                    store=self.store,
                    attachment_paths=att_paths,
                    media_type=dm_media,
                    ratio=dm_ratio,
                    duration_sec=dm_dur,
                )
                dm_delivered_files: list[str] = []
                for d_path in res_dm.deliverable_paths:
                    try:
                        att_ret = await self.bridge.send_attachment(reply_recipient, d_path)
                        if att_ret is not False:
                            dm_delivered_files.append(d_path.name)
                    except Exception:
                        logger.warning(
                            "Failed sending run_dreamina_canvas deliverable %s",
                            d_path.name,
                            exc_info=True,
                        )
                return json.dumps(
                    {
                        "ok": res_dm.ok,
                        "media_type": res_dm.media_type,
                        "mode": res_dm.mode,
                        "model_used": res_dm.model_used,
                        "project_id": res_dm.project_id,
                        "web_url": res_dm.web_url,
                        "node_id": res_dm.node_id,
                        "delivered_attachments": dm_delivered_files,
                        "reply": res_dm.reply,
                        "formatted_reply": res_dm.formatted_reply,
                        "error": res_dm.error,
                    },
                    ensure_ascii=False,
                )

            if fn_name == "fetch_url_text":
                url = str(args.get("url", "")).strip()
                content = await _simple_fetch_url(url)
                return json.dumps({"ok": True, "url": url, "content": content}, ensure_ascii=False)

            return json.dumps({"ok": False, "error": f"未知工具: {fn_name}"}, ensure_ascii=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Error executing local tool %s", fn_name)
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @staticmethod
    def _extract_response_fields(
        choice_message: Any,
    ) -> tuple[str, str, list[dict[str, Any]] | None, list[dict[str, Any]] | None]:
        """Extract (content, reasoning_content, tool_calls, server_tool_calls) safely."""
        content = getattr(choice_message, "content", None)
        content_str = content if isinstance(content, str) else ""

        reasoning = getattr(choice_message, "reasoning_content", None)
        reasoning_str = reasoning if isinstance(reasoning, str) else ""

        raw_tcs = getattr(choice_message, "tool_calls", None)
        norm_tcs: list[dict[str, Any]] | None = None
        if raw_tcs:
            norm_tcs = []
            for tc in raw_tcs:
                if isinstance(tc, dict):
                    fn = tc.get("function", {})
                    norm_tcs.append(
                        {
                            "id": str(tc.get("id", "")),
                            "type": str(tc.get("type", "function")),
                            "function": {
                                "name": str(fn.get("name", "")),
                                "arguments": str(fn.get("arguments", "{}")),
                            },
                        }
                    )
                else:
                    fn_obj = getattr(tc, "function", None)
                    norm_tcs.append(
                        {
                            "id": str(getattr(tc, "id", "")),
                            "type": str(getattr(tc, "type", "function")),
                            "function": {
                                "name": str(getattr(fn_obj, "name", "")),
                                "arguments": str(getattr(fn_obj, "arguments", "{}")),
                            },
                        }
                    )

        raw_stcs = getattr(choice_message, "server_tool_calls", None)
        if raw_stcs is None and hasattr(choice_message, "model_extra"):
            extra = getattr(choice_message, "model_extra") or {}
            if isinstance(extra, dict):
                raw_stcs = extra.get("server_tool_calls")
                if not reasoning_str and isinstance(extra.get("reasoning_content"), str):
                    reasoning_str = extra["reasoning_content"]

        server_tool_calls: list[dict[str, Any]] | None = None
        if raw_stcs:
            normalized = _normalize_jsonable(raw_stcs)
            if isinstance(normalized, list):
                server_tool_calls = normalized

        return (content_str, reasoning_str, norm_tcs, server_tool_calls)

    async def _run_agent_turn(
        self,
        sender: str,
        user_text: str,
        image_data_urls: list[str] | None = None,
        turn_ctx: dict[str, Any] | None = None,
        attachment_paths: list[str] | None = None,
    ) -> str:
        active_channels = (
            detect_explicit_channels(user_text) if self._is_admin_sender(sender) else []
        )
        is_pure_single_channel = len(active_channels) == 1 and not is_local_mac_compound_action(
            user_text
        )

        # Direct owner-only fast-path for pure Dreamina Canvas (`即梦画布`) requests
        if is_pure_single_channel and active_channels[0] == "dreamina":
            if turn_ctx is not None:
                turn_ctx["current_stage"] = "正在用即梦画布生成多媒体产物"
            dm_query = extract_dreamina_query(user_text)
            eff_att_paths = (
                attachment_paths
                if attachment_paths is not None
                else (
                    list(turn_ctx.get("attachment_paths") or [])
                    if turn_ctx is not None
                    else None
                )
            )
            async with self._db_lock:
                self.store.append_conversation_message(
                    sender=sender, role="user", content=user_text
                )
            dm_res = await self.dreamina_client.ask(
                dm_query,
                sender=sender,
                store=self.store,
                attachment_paths=eff_att_paths,
            )
            if dm_res.deliverable_paths:
                if turn_ctx is not None:
                    turn_ctx.setdefault("deferred_attachments", []).extend(
                        [("", p) for p in dm_res.deliverable_paths]
                    )
                else:
                    for d_path in dm_res.deliverable_paths:
                        with contextlib.suppress(Exception):
                            await self.bridge.send_attachment(sender, d_path)

            final_dm_text = dm_res.formatted_reply or (
                "即梦画布已生成结果，请查收附件。"
                if dm_res.deliverable_paths
                else "即梦画布已处理完成。"
            )
            async with self._db_lock:
                self.store.append_conversation_message(
                    sender=sender, role="assistant", content=final_dm_text
                )
            return final_dm_text

        # Direct owner-only fast-path for pure DSH / Codex / AGY requests
        if is_pure_single_channel and active_channels[0] in ("dsh", "codex", "agy"):
            ch_key = active_channels[0]
            h_map: dict[str, tuple[str, Any, Any]] = {
                "dsh": ("DSH", self.dsh_client, extract_dsh_query),
                "codex": ("Codex", self.codex_client, extract_codex_query),
                "agy": ("AGY", self.agy_client, extract_agy_query),
            }
            h_label, h_client, extract_fn = h_map[ch_key]
            if turn_ctx is not None:
                turn_ctx["current_stage"] = f"等待 {h_label} 本机工作台处理"
            h_query = extract_fn(user_text) or (user_text or "").strip()
            async with self._db_lock:
                self.store.append_conversation_message(
                    sender=sender, role="user", content=user_text
                )
            h_res = await h_client.ask(h_query, sender=sender, store=self.store)
            if h_res.deliverable_paths:
                if turn_ctx is not None:
                    turn_ctx.setdefault("deferred_attachments", []).extend(
                        [("", p) for p in h_res.deliverable_paths]
                    )
                else:
                    for d_path in h_res.deliverable_paths:
                        with contextlib.suppress(Exception):
                            await self.bridge.send_attachment(sender, d_path)
            final_h_text = h_res.formatted_reply or (
                f"{h_label} 已生成结果文件，请查收附件。"
                if h_res.deliverable_paths
                else f"{h_label} 已处理完成。"
            )
            async with self._db_lock:
                self.store.append_conversation_message(
                    sender=sender, role="assistant", content=final_h_text
                )
            return final_h_text

        explicit_xiaona = "xiaona" in active_channels
        explicit_workbuddy = "workbuddy" in active_channels

        # Direct owner-only WorkBuddy fast-path for pure explicit requests
        if explicit_workbuddy and not is_compound_workbuddy_request(user_text):
            if turn_ctx is not None:
                turn_ctx["current_stage"] = "等待 WorkBuddy 本机工作台处理"
            wb_query = extract_workbuddy_query(user_text) or (user_text or "").strip()
            async with self._db_lock:
                self.store.append_conversation_message(
                    sender=sender, role="user", content=user_text
                )
            wb_res = await self.workbuddy_client.ask(
                wb_query, sender=sender, store=self.store
            )
            if wb_res.deliverable_paths:
                if turn_ctx is not None:
                    turn_ctx.setdefault("deferred_attachments", []).extend(
                        [("", p) for p in wb_res.deliverable_paths]
                    )
                else:
                    for d_path in wb_res.deliverable_paths:
                        with contextlib.suppress(Exception):
                            await self.bridge.send_attachment(sender, d_path)

            final_wb_text = wb_res.formatted_reply or (
                "WorkBuddy 已生成结果文件，请查收附件。"
                if wb_res.deliverable_paths
                else "WorkBuddy 已处理完成。"
            )
            async with self._db_lock:
                self.store.append_conversation_message(
                    sender=sender, role="assistant", content=final_wb_text
                )
            return final_wb_text

        # Direct owner-only XiaoNa / XiaoMiGuan fast-path for pure explicit requests
        if explicit_xiaona and not is_compound_xiaona_request(user_text):
            alias_label = "小蜜罐" if is_xiaomiguan_alias_used(user_text) else "小纳"
            if turn_ctx is not None:
                turn_ctx["current_stage"] = f"等待{alias_label}工作台回复"
            xiaona_query = extract_xiaona_query(user_text) or (user_text or "").strip()
            async with self._db_lock:
                self.store.append_conversation_message(
                    sender=sender, role="user", content=user_text
                )
            xn_res = await self.xiaona_client.ask(
                xiaona_query, sender=sender, store=self.store
            )

            downloaded_pairs: list[tuple[str, Path]] = []
            fallback_urls: list[str] = []
            if xn_res.media_urls:
                if turn_ctx is not None:
                    turn_ctx["current_stage"] = f"正在接收{alias_label}发来的文件"
                for media_u in xn_res.media_urls:
                    local_media = await self.xiaona_client.download_media_url(media_u)
                    if local_media is not None:
                        if turn_ctx is not None:
                            downloaded_pairs.append((media_u, local_media))
                        else:
                            sent_ok = False
                            try:
                                att_ret = await self.bridge.send_attachment(sender, local_media)
                                sent_ok = att_ret is not False
                            except Exception:
                                sent_ok = False
                            if sent_ok:
                                downloaded_pairs.append((media_u, local_media))
                            else:
                                fallback_urls.append(media_u)
                    else:
                        fallback_urls.append(media_u)
                if turn_ctx is not None and downloaded_pairs:
                    turn_ctx.setdefault("deferred_attachments", []).extend(downloaded_pairs)

            delivered_urls = [u for u, _ in downloaded_pairs]
            clean_reply_raw = strip_delivered_urls_from_text(xn_res.reply, delivered_urls)
            final_xn_text = format_xiaona_reply(clean_reply_raw, fallback_urls)
            if not final_xn_text:
                final_xn_text = (
                    "文件已发你，请查收。" if downloaded_pairs else f"{alias_label}已处理完成。"
                )
            async with self._db_lock:
                self.store.append_conversation_message(
                    sender=sender, role="assistant", content=final_xn_text
                )
            return final_xn_text

        include_xiaona = explicit_xiaona
        include_workbuddy = explicit_workbuddy
        include_dsh = "dsh" in active_channels
        include_codex = "codex" in active_channels
        include_agy = "agy" in active_channels
        include_dreamina = "dreamina" in active_channels
        provider_name, api_key, base_url, is_deepseek_native = (
            self.config.resolve_active_provider()
        )
        if (
            provider_name == "unconfigured"
            and self._injected_llm_client is None
        ):
            return (
                "⚠️ 当前尚未配置可用的模型密钥。\n"
                "请打开项目目录的 .env 文件，填入真实的 DEEPSEEK_API_KEY 或 OPENAI_API_KEY。"
            )

        if self._injected_llm_client is not None and provider_name == "unconfigured":
            is_deepseek_native = self.config.provider_mode == "deepseek"
            provider_name = "deepseek" if is_deepseek_native else "openai"

        model_name = (
            self.config.deepseek_model if is_deepseek_native else self.config.openai_model
        )
        client = self._get_or_create_client(api_key, base_url, is_fallback=False)

        sys_prompt = await self._build_system_prompt(
            sender,
            user_text,
            include_xiaona=include_xiaona,
            include_workbuddy=include_workbuddy,
            include_dsh=include_dsh,
            include_codex=include_codex,
            include_agy=include_agy,
            include_dreamina=include_dreamina,
        )

        if image_data_urls:
            user_content: Any = [{"type": "text", "text": user_text}] + [
                {"type": "image_url", "image_url": {"url": u}} for u in image_data_urls
            ]
        else:
            user_content = user_text

        async with self._db_lock:
            history = self.store.get_conversation_history(
                sender=sender,
                limit=self.config.max_history_messages,
                is_deepseek_native=is_deepseek_native,
            )
            self.store.append_conversation_message(
                sender=sender, role="user", content=user_content
            )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": sys_prompt},
            *history,
            {"role": "user", "content": user_content},
        ]
        tools = self._build_tools(
            is_deepseek_native=is_deepseek_native,
            include_xiaona=include_xiaona,
            include_workbuddy=include_workbuddy,
            include_dsh=include_dsh,
            include_codex=include_codex,
            include_agy=include_agy,
            include_dreamina=include_dreamina,
        )

        for round_idx in range(self.config.max_tool_rounds):
            req_kwargs: dict[str, Any] = {
                "model": model_name,
                "messages": messages,
                "tools": tools,
            }
            if is_deepseek_native:
                effort = (self.config.deepseek_reasoning_effort or "high").strip().lower()
                if effort == "none":
                    req_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
                else:
                    req_kwargs["reasoning_effort"] = effort
                    req_kwargs["extra_body"] = {"thinking": {"type": "enabled"}}

            try:
                response = await client.chat.completions.create(**req_kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as primary_exc:
                can_fallback = (
                    is_deepseek_native
                    and round_idx == 0
                    and (
                        bool(self.config.openai_api_key)
                        or self._injected_fallback_client is not None
                    )
                )
                if not can_fallback:
                    logger.exception("LLM request failed for sender=%s", sender)
                    return f"⚠️ 刚刚处理时出了点状况：{primary_exc}"

                logger.warning(
                    "Primary DeepSeek call failed (%s); switching to isolated OpenAI fallback.",
                    primary_exc,
                )
                is_deepseek_native = False
                model_name = self.config.openai_model
                client = self._get_or_create_client(
                    self.config.openai_api_key,
                    self.config.openai_base_url,
                    is_fallback=True,
                )
                async with self._db_lock:
                    clean_history = self.store.get_conversation_history(
                        sender=sender,
                        limit=self.config.max_history_messages,
                        is_deepseek_native=False,
                    )
                messages = [{"role": "system", "content": sys_prompt}, *clean_history]
                tools = self._build_tools(
                    is_deepseek_native=False,
                    include_xiaona=include_xiaona,
                    include_workbuddy=include_workbuddy,
                    include_dsh=include_dsh,
                    include_codex=include_codex,
                    include_agy=include_agy,
                    include_dreamina=include_dreamina,
                )
                response = await client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    tools=tools,
                )

            choice_msg = response.choices[0].message
            content_str, reasoning_str, tool_calls, server_tool_calls = (
                self._extract_response_fields(choice_msg)
            )

            async with self._db_lock:
                self.store.append_conversation_message(
                    sender=sender,
                    role="assistant",
                    content=content_str,
                    tool_calls=tool_calls,
                    reasoning_content=reasoning_str,
                    server_tool_calls=server_tool_calls,
                )

            assistant_wire: dict[str, Any] = {
                "role": "assistant",
                "content": content_str,
            }
            if tool_calls:
                assistant_wire["tool_calls"] = tool_calls
            if is_deepseek_native:
                assistant_wire["reasoning_content"] = reasoning_str
                if server_tool_calls:
                    assistant_wire["server_tool_calls"] = server_tool_calls
            messages.append(assistant_wire)

            if not tool_calls:
                return content_str.strip() or "好嘞，已经办妥了。"

            # Principle 2 (Zero Plumbing on Screen): intermediate `content_str` returned alongside
            # `tool_calls` is persisted in conversation history ONLY and NEVER sent as an iMessage bubble.
            if turn_ctx is not None:
                called_names = {tc["function"]["name"] for tc in tool_calls}
                if "create_and_send_document" in called_names:
                    turn_ctx["current_stage"] = "整理结论并生成排版附件"
                elif "run_browser_task" in called_names:
                    turn_ctx["current_stage"] = "正在页面上核对信息"
                elif "ask_xiaona" in called_names:
                    turn_ctx["current_stage"] = "等待小纳工作台回复"
                elif "ask_workbuddy" in called_names:
                    turn_ctx["current_stage"] = "等待 WorkBuddy 本机工作台处理"
                elif "ask_dsh" in called_names:
                    turn_ctx["current_stage"] = "等待 DSH 本机工作台处理"
                elif "ask_codex" in called_names:
                    turn_ctx["current_stage"] = "等待 Codex 本机工作台处理"
                elif "ask_agy" in called_names:
                    turn_ctx["current_stage"] = "等待 AGY 本机工作台处理"
                elif "run_dreamina_canvas" in called_names:
                    turn_ctx["current_stage"] = "正在用即梦画布生成多媒体产物"

            for tc in tool_calls:
                tc_id = tc["id"]
                fn_name = tc["function"]["name"]
                fn_args = tc["function"]["arguments"]
                tool_output = await self._execute_local_tool(
                    sender, fn_name, fn_args, turn_ctx=turn_ctx
                )
                async with self._db_lock:
                    self.store.append_conversation_message(
                        sender=sender,
                        role="tool",
                        content=tool_output,
                        tool_call_id=tc_id,
                        name=fn_name,
                    )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": fn_name,
                        "content": tool_output,
                    }
                )

        return "⚠️ 这件事步骤有点多，我先停在这儿，你可以告诉我下一步重点弄哪块。"

    async def notify_interrupted_imessage(self, msg: InboundMessage) -> None:
        _, recipient, is_admin = self._resolve_session_and_reply(msg)
        if is_admin or self.store.is_authorized_user(msg.sender):
            await self.bridge.send_text(
                recipient,
                "我在处理一条请求时意外中断了。这条请求可能已执行，请先核对结果，再决定是否重发。",
            )

    async def handle_inbound_message(self, msg: InboundMessage) -> str | None:
        """Handle inbound user message: cross-alias 2FA unlock -> auth gate -> live interruption -> stage-aware turn."""
        session_sender, reply_recipient, is_admin = self._resolve_session_and_reply(msg)
        raw_norm = normalize_sender(msg.sender)
        session_key = normalize_sender(session_sender) or raw_norm

        if self.config.allow_self_chat and is_admin:
            gate_candidates = [
                session_sender,
                session_key,
                raw_norm,
                msg.sender,
                *self.config.admin_senders_ordered,
            ]
        else:
            gate_candidates = [session_sender, session_key, raw_norm, msg.sender]

        stripped = (msg.text or "").strip()
        is_stop = _is_stop_command(stripped)
        is_status = _is_status_command(stripped)

        if self.store.is_revoked_user(raw_norm):
            return None
        is_member = (not is_admin) and self.store.is_authorized_user(raw_norm)

        # Explicit requests and controls retain their meaning while a gate is open.
        # A polled approval must match the same gate, even after a timeout or replacement.
        if msg.approval_only:
            if (is_admin or is_member) and msg.approval_token is not None:
                self.human_gate.try_resolve_any(gate_candidates, msg.text, token=msg.approval_token)
            return None
        if (
            not (is_stop or is_status) and (is_admin or is_member)
            and not isinstance(self.bridge, MacIMessageBridge)
        ):
            answer = approval_answer(stripped)
            if answer is not None and (
                self.human_gate.try_resolve_any(gate_candidates, answer)
                or is_explicit_approval_reply(stripped)
            ):
                return None

        # 2. Multi-User Invite Beta authorization & self-service redemption gate
        if not is_admin and not is_member:
            if self.config.enable_invite_codes:
                redeemed, _ = self.store.redeem_invite_code(raw_norm, msg.text or "")
                if redeemed:
                    welcome = (
                        "🎉 邀请码激活成功！已为你分配独立记忆空间与专属浏览器沙盒，直接发消息即可开始使用。"
                    )
                    await self.bridge.send_text(reply_recipient, welcome)
                    return welcome
                if self.store.should_send_invite_prompt(raw_norm):
                    prompt_msg = (
                        "👋 你好，当前 AI 助理处于邀请内测阶段，请直接回复邀请码（如 HT-XXXXXX）完成激活。"
                    )
                    await self.bridge.send_text(reply_recipient, prompt_msg)
                    return prompt_msg
            return None

        # Hard Admin/Owner-Only Gate for all 6 specialist channels (zero HTTP/CLI/LLM calls)
        if not is_admin:
            non_admin_channels = detect_explicit_channels(stripped)
            if non_admin_channels:
                first_ch = non_admin_channels[0]
                if first_ch == "xiaona":
                    alias_name = "小蜜罐" if is_xiaomiguan_alias_used(stripped) else "小纳"
                    deny_msg = (
                        f"{alias_name}连接的是主理人私人工作台，仅限主理人本人调用；其他问题我可以直接帮你处理。"
                    )
                elif first_ch == "dreamina":
                    deny_msg = (
                        "即梦画布连接的是主理人本机账号与积分池，仅限主理人本人调用；其他问题我可以直接帮你处理。"
                    )
                else:
                    ch_title = {
                        "workbuddy": "WorkBuddy",
                        "dsh": "DSH",
                        "codex": "Codex",
                        "agy": "AGY",
                    }[first_ch]
                    deny_msg = (
                        f"{ch_title} 连接的是主理人本机工作台，仅限主理人本人调用；其他问题我可以直接帮你处理。"
                    )
                await self.bridge.send_text(reply_recipient, deny_msg)
                return deny_msg

        if stripped.lower() in ("/ping", "ping"):
            reply = "🏓 HyperText 助理在线，随时吩咐。"
            await self.bridge.send_text(reply_recipient, reply)
            return reply

        if stripped.lower() in ("/clear", "/reset"):
            async with self._db_lock:
                self.store.clear_conversation(session_sender)
            reply = "🧹 对话上下文已清空，长期记忆仍为你保留。"
            await self.bridge.send_text(reply_recipient, reply)
            return reply

        # Look up the newest unfinished turn; earlier concurrent turns stay reachable.
        if is_stop or is_status:
            current = next(
                (ctx for ctx in reversed(self._active_turns.get(session_key, []))
                 if not ctx["task"].done() and not ctx["cancelled_by_user"]),
                None,
            )
            if current is None:
                reply = "现在没有正在处理的对话任务。"
            elif is_stop:
                current["cancelled_by_user"] = True
                current["task"].cancel()
                reply = "好，刚才那件事已经停下了。有新安排你随时说。"
                if isinstance(self.bridge, MacIMessageBridge) and self.bridge.durable_inbox:
                    rowid = current["inbound_rowid"]
                    try:
                        await asyncio.to_thread(self.store.complete_imessage, rowid)
                    except sqlite3.Error:
                        logger.exception("Could not record cancellation of iMessage row %d", rowid)
                        reply = "已请求停止。当前无法记录停止结果，请稍后查一下进度。"
            else:
                elapsed = max(1, int(time.time() - float(current["started_at"])))
                task_label = current["summary"]
                stage_now = current.get("current_stage", "正在检索核对资料")
                if self.human_gate.has_pending_any(gate_candidates):
                    stage_now = "正在等你确认"
                reply = (
                    f"还在办「{task_label}」（{stage_now}，已用时约 {elapsed} 秒），"
                    "有结果马上发你。要取消的话，回个「停」就行。"
                )
            await self.bridge.send_text(reply_recipient, reply)
            return reply

        # 3b. Admin Explicit Multi-Harness Matrix Reset & Pre-Flight Guards (v0.6.0)
        admin_channels = detect_explicit_channels(stripped) if is_admin else []
        is_explicit_xn = "xiaona" in admin_channels
        is_explicit_wb = "workbuddy" in admin_channels
        alias_xn = "小蜜罐" if is_xiaomiguan_alias_used(stripped) else "小纳"

        if len(admin_channels) == 1:
            sole_ch = admin_channels[0]
            if sole_ch == "workbuddy":
                if is_workbuddy_reset_command(stripped):
                    async with self._db_lock:
                        self.workbuddy_client.reset_session(self.store, session_sender)
                    reset_wb = "🔄 已重置 WorkBuddy 会话上下文，下一条消息将开启全新对话。"
                    await self.bridge.send_text(reply_recipient, reset_wb)
                    return reset_wb
                if not extract_workbuddy_query(stripped):
                    empty_wb = (
                        "请直接附上要交给 WorkBuddy 处理的任务，例如：/wb 帮我生成一份竞品对比 HTML 卡片"
                    )
                    await self.bridge.send_text(reply_recipient, empty_wb)
                    return empty_wb

            elif sole_ch == "dsh":
                if is_dsh_reset_command(stripped):
                    async with self._db_lock:
                        self.dsh_client.reset_session(self.store, session_sender)
                    reset_dsh = "🔄 已重置 DSH 会话上下文，下一条消息将开启全新对话。"
                    await self.bridge.send_text(reply_recipient, reset_dsh)
                    return reset_dsh
                if not extract_dsh_query(stripped):
                    empty_dsh = "请直接附上要交给 DSH 处理的任务，例如：/dsh 帮我分析这段架构方案"
                    await self.bridge.send_text(reply_recipient, empty_dsh)
                    return empty_dsh

            elif sole_ch == "codex":
                if is_codex_reset_command(stripped):
                    async with self._db_lock:
                        self.codex_client.reset_session(self.store, session_sender)
                    reset_cdx = "🔄 已重置 Codex 会话上下文，下一条消息将开启全新对话。"
                    await self.bridge.send_text(reply_recipient, reset_cdx)
                    return reset_cdx
                if not extract_codex_query(stripped):
                    empty_cdx = "请直接附上要交给 Codex 处理的任务，例如：/codex 帮我审查这段并发逻辑"
                    await self.bridge.send_text(reply_recipient, empty_cdx)
                    return empty_cdx

            elif sole_ch == "agy":
                if is_agy_reset_command(stripped):
                    async with self._db_lock:
                        self.agy_client.reset_session(self.store, session_sender)
                    reset_agy = "🔄 已重置 AGY 会话上下文，下一条消息将开启全新对话。"
                    await self.bridge.send_text(reply_recipient, reset_agy)
                    return reset_agy
                if not extract_agy_query(stripped):
                    empty_agy = "请直接附上要交给 AGY 处理的任务，例如：/agy 帮我设计一套前端状态机方案"
                    await self.bridge.send_text(reply_recipient, empty_agy)
                    return empty_agy

            elif sole_ch == "dreamina":
                if is_dreamina_reset_command(stripped):
                    async with self._db_lock:
                        self.dreamina_client.reset_canvas(self.store, session_sender)
                    reset_dm = "🎨 已重置即梦画布上下文，下一条生成指令将自动创建全新画布。"
                    await self.bridge.send_text(reply_recipient, reset_dm)
                    return reset_dm
                if not extract_dreamina_query(stripped) and not msg.attachment_paths:
                    empty_dm = (
                        "请直接描述你想用即梦生成的画面、视频或音乐，"
                        "例如：/即梦 一只戴宇航头盔的橘猫在月球喝咖啡，电影感海报"
                    )
                    await self.bridge.send_text(reply_recipient, empty_dm)
                    return empty_dm

        if is_explicit_xn:
            if len(admin_channels) == 1 and is_xiaona_reset_command(stripped):
                async with self._db_lock:
                    self.xiaona_client.reset_conversation(self.store, session_sender)
                reset_reply = f"🔄 已重置{alias_xn}会话上下文，下一条消息将开启全新对话。"
                await self.bridge.send_text(reply_recipient, reset_reply)
                return reset_reply

            xn_url, xn_key, _ = self.xiaona_client.resolve_credentials()
            if not xn_url or not xn_key:
                env_hint = str(
                    self.config.loaded_env_file
                    or (Path(__file__).resolve().parent.parent / ".env")
                )
                hint_reply = (
                    f"{alias_xn}通道已接入，还差一步配置密钥：把 XIAONA_RELAY_KEY=xn_... 写入 {env_hint}"
                    "（或存入钥匙串 XIAONA_RELAY_KEY）即可免重启直接调用。"
                )
                await self.bridge.send_text(reply_recipient, hint_reply)
                return hint_reply

            if len(admin_channels) == 1 and not extract_xiaona_query(stripped):
                if alias_xn == "小蜜罐":
                    empty_reply = "请直接附上要问小蜜罐的内容，例如：/小蜜罐 今天有什么日程"
                else:
                    empty_reply = "请直接附上要问小纳的内容，例如：/xiaona 今天有什么日程"
                await self.bridge.send_text(reply_recipient, empty_reply)
                return empty_reply

        # 4. Per-user daily quota enforcement (admins exempt)
        async with self._db_lock:
            allowed_q, quota_reason = self.store.check_and_increment_quota(
                session_key, kind="msg", is_admin=is_admin
            )
        if not allowed_q:
            await self.bridge.send_text(reply_recipient, quota_reason)
            return quota_reason

        # 5. Launch tracked turn task + Single Initial Ack + Capped Low-Frequency Reassurance (v0.6.0)
        has_images = bool(msg.image_data_urls)
        summary = _summarize_task_label(msg.text, has_images)
        ack_text = (
            self._craft_instant_ack(msg.text, has_images=has_images)
            if self.config.enable_instant_ack
            else ""
        )
        if len(admin_channels) == 1 and not is_local_mac_compound_action(stripped):
            ch_stage_map = {
                "xiaona": f"等待{alias_xn}工作台回复",
                "workbuddy": "等待 WorkBuddy 本机工作台处理",
                "dsh": "等待 DSH 本机工作台处理",
                "codex": "等待 Codex 本机工作台处理",
                "agy": "等待 AGY 本机工作台处理",
                "dreamina": "正在用即梦画布生成多媒体产物",
            }
            initial_stage = ch_stage_map[admin_channels[0]]
        elif len(admin_channels) >= 2:
            initial_stage = "正在并行调度多智能体方案对比"
        else:
            initial_stage = "正在检索核对资料"

        turn_ctx: dict[str, Any] = {
            "summary": summary,
            "inbound_rowid": msg.rowid,
            "started_at": time.time(),
            "ack_sent": False,
            "ack_sent_at": 0.0,
            "has_ack_timer": bool(ack_text),
            "cancelled_by_user": False,
            "current_stage": initial_stage,
            "browser_progress_state": None,
            "pre_doc_notice_sent": False,
            "deferred_attachments": [],
            "attachment_paths": list(msg.attachment_paths or []),
            "reply_recipient": reply_recipient,
        }

        turn_task: asyncio.Task[str] = asyncio.create_task(
            self._run_agent_turn(
                sender=session_sender,
                user_text=msg.text,
                image_data_urls=msg.image_data_urls,
                turn_ctx=turn_ctx,
                attachment_paths=msg.attachment_paths,
            )
        )
        turn_ctx["task"] = turn_task
        self._active_turns.setdefault(session_key, []).append(turn_ctx)

        ack_task: asyncio.Task[None] | None = None
        if ack_text:
            is_fast_ack = _is_heavy_browser_intent(msg.text) or bool(admin_channels)
            if self.config.fast_ack_delay_sec <= 0:
                ack_delay = 0.0
            elif is_fast_ack:
                ack_delay = min(0.4, float(self.config.fast_ack_delay_sec))
            else:
                ack_delay = float(self.config.fast_ack_delay_sec)

            async def _ack_worker() -> None:
                if ack_delay > 0:
                    await asyncio.sleep(ack_delay)
                if (
                    not turn_task.done()
                    and not turn_ctx.get("ack_sent")
                    and not self.human_gate.has_pending_any(gate_candidates)
                ):
                    turn_ctx["ack_sent"] = True
                    turn_ctx["ack_sent_at"] = time.time()
                    await self.bridge.send_text(reply_recipient, ack_text)
                    turn_ctx["ack_sent_at"] = time.time()

            ack_task = asyncio.create_task(_ack_worker())

        heartbeat_task: asyncio.Task[None] | None = None
        hb_interval = float(self.config.heartbeat_interval_sec)
        max_hb = max(0, int(self.config.max_heartbeat_per_turn))
        if hb_interval > 0 and max_hb > 0:

            async def _heartbeat_worker() -> None:
                heartbeats_sent = 0
                last_hb_anchor = 0.0
                while not turn_task.done() and heartbeats_sent < max_hb:
                    anchor = max(
                        last_hb_anchor,
                        float(turn_ctx["started_at"]),
                        float(turn_ctx.get("ack_sent_at") or 0.0),
                    )
                    remaining = (anchor + hb_interval) - time.time()
                    if remaining > 0:
                        await asyncio.sleep(remaining)
                        continue
                    if turn_task.done():
                        break
                    # Re-check anchor on wakeup in case `_ack_worker` or browser notice updated `ack_sent_at`
                    anchor = max(
                        last_hb_anchor,
                        float(turn_ctx["started_at"]),
                        float(turn_ctx.get("ack_sent_at") or 0.0),
                    )
                    if (anchor + hb_interval) - time.time() > 0:
                        continue
                    if self.human_gate.has_pending_any(gate_candidates):
                        await asyncio.sleep(min(0.5, hb_interval))
                        continue
                    heartbeats_sent += 1
                    reassurance_msg = (
                        "内容稍微有点多，还在做最后的核对整理，马上好。"
                        if heartbeats_sent == 1
                        else "还在做最后的细节核对与收尾，请再稍等片刻，马上发你。"
                    )
                    await self.bridge.send_text(reply_recipient, reassurance_msg)
                    last_hb_anchor = time.time()

            heartbeat_task = asyncio.create_task(_heartbeat_worker())

        try:
            raw_reply = await turn_task
        except asyncio.CancelledError:
            if not turn_task.done():
                turn_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await turn_task
            if turn_ctx.get("cancelled_by_user"):
                return None
            raise
        finally:
            active = self._active_turns[session_key]
            active.remove(turn_ctx)
            if not active:
                self._active_turns.pop(session_key)
            helpers = [task for task in (ack_task, heartbeat_task) if task is not None]
            for helper in helpers:
                if not helper.done():
                    helper.cancel()
            await asyncio.gather(*helpers, return_exceptions=True)

        formatted_reply = format_for_imessage(raw_reply)
        if formatted_reply:
            await self._deliver_formatted_reply(reply_recipient, formatted_reply)

        for orig_url, local_path in turn_ctx.get("deferred_attachments") or []:
            sent_ok = False
            try:
                att_res = await self.bridge.send_attachment(reply_recipient, local_path)
                sent_ok = att_res is not False
            except Exception:
                logger.warning(
                    "Failed sending XiaoNa media attachment %s; falling back to URL",
                    local_path.name,
                    exc_info=True,
                )
                sent_ok = False
            if not sent_ok and orig_url:
                await self.bridge.send_text(reply_recipient, orig_url)

        return formatted_reply

    async def process_due_tasks_once(self, now_ts: float | None = None) -> int:
        """Claim and execute all due scheduled tasks (direct_notify & agent_wakeup)."""
        async with self._db_lock:
            due_tasks = self.store.claim_due_tasks(now_ts=now_ts)
        executed = 0
        for task in due_tasks:
            raw_task_sender = task["sender"]
            is_admin = self._is_admin_sender(raw_task_sender)
            if not is_admin and not self.store.is_authorized_user(raw_task_sender):
                self.store.cancel_scheduled_task(task["id"], sender=raw_task_sender)
                continue
            reply_to = (
                (self.config.self_chat_reply_to.strip() or raw_task_sender)
                if self.config.allow_self_chat and is_admin else raw_task_sender
            )
            task_type = task["task_type"]
            payload = task["payload"]
            tid = task["id"]
            try:
                if task_type == "direct_notify":
                    clean_payload = format_for_imessage(payload)
                    await self.bridge.send_text(reply_to, f"⏰ [定时提醒] {clean_payload}")
                    executed += 1
                elif task_type == "agent_wakeup":
                    async with self._db_lock:
                        allowed, _ = self.store.check_and_increment_quota(
                            raw_task_sender, kind="msg", is_admin=is_admin
                        )
                    if not allowed:
                        continue
                    wakeup_prompt = (
                        f"[系统定时唤醒任务 #{tid}] 请执行以下预定任务并按贴身助理简洁短信风格直接输出汇报：\n{payload}"
                    )
                    executed += 1
                    reply = await self._run_agent_turn(
                        sender=raw_task_sender, user_text=wakeup_prompt
                    )
                    clean_reply = format_for_imessage(reply)
                    if clean_reply and not self.store.is_revoked_user(raw_task_sender):
                        await self.bridge.send_text(reply_to, f"🔔 [定时汇报]\n{clean_reply}")
            except Exception:
                logger.exception("Failed executing scheduled task id=%s", tid)
        return executed


# Backward-compatibility alias
InstinctOrchestrator = HyperTextOrchestrator


async def proactive_cron_loop(
    orchestrator: HyperTextOrchestrator,
    interval_sec: float = 15.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Background coroutine polling SQLite scheduled_tasks periodically."""
    sleep_sec = max(0.2, float(interval_sec))
    while stop_event is None or not stop_event.is_set():
        try:
            await orchestrator.process_due_tasks_once()
        except Exception:
            logger.exception("Unexpected error in proactive_cron_loop")
        if stop_event is not None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=sleep_sec)
                break
            except asyncio.TimeoutError:
                continue
        else:
            await asyncio.sleep(sleep_sec)
