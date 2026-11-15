"""XiaoNa Cognitive Orchestrator: Instant Ack, Live Interruption, Hybrid Memory, DeepSeek/OpenAI Routing & Attachments."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import html
import json
import logging
import re
import time
import urllib.request
from pathlib import Path
from typing import Any

from .bridge_imessage import InboundMessage, MessageBridge
from .config import AgentConfig, normalize_sender
from .formatter import (
    format_for_imessage,
    is_sensitive_attachment_path,
    render_mobile_html_card,
    sanitize_attachment_filename,
)
from .human_gate import HumanGate
from .sandbox_browser import BrowserSandbox
from .store_sqlite import StateStore

logger = logging.getLogger(__name__)

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

_STOP_COMMANDS = {
    "停",
    "停一下",
    "停下",
    "停止",
    "取消",
    "算了",
    "别查了",
    "别弄了",
    "不用查了",
    "不用弄了",
    "stop",
    "cancel",
    "/stop",
}

_STATUS_COMMANDS = {
    "进度",
    "什么进度",
    "在干嘛",
    "在干什么",
    "好了吗",
    "弄好了吗",
    "还在弄吗",
    "还在查吗",
    "办得怎么样",
    "办得怎么样了",
    "怎么样了",
    "status",
    "/status",
}


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


def _clean_command_phrase(text: str) -> str:
    return re.sub(r"[\s!！?？.。,，~～、]+", "", (text or "").strip()).lower()


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


class InstinctOrchestrator:
    """XiaoNa conversational orchestrator with instant ack, live interruption, hybrid memory & attachments."""

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
        self._injected_llm_client = llm_client
        self._injected_fallback_client = fallback_llm_client
        self._injected_embedding_client = embedding_client
        self._client_cache: dict[tuple[str, str], Any] = {}
        self._db_lock = asyncio.Lock()
        self._active_turns: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _craft_instant_ack(user_text: str, has_images: bool = False) -> str:
        """Craft a warm, natural, zero-jargon human assistant acknowledgment tailored to user intent."""
        txt = (user_text or "").strip()
        low = txt.lower()

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
        if norm == "local-cli-user":
            return True
        if norm in self.config.allowed_senders:
            return True
        # Backward compatibility for embedded tests where allowed_senders was left empty and invite mode is off
        if not self.config.allowed_senders and not self.config.enable_invite_codes:
            return True
        return False

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

    def _build_tools(self, is_deepseek_native: bool) -> list[dict[str, Any]]:
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
                                "description": "完整的正文内容（当 doc_format='html' 时可传入结构化段落/列表/表格，系统会自动渲染为精美自适应手机卡片）",
                            },
                            "doc_format": {
                                "type": "string",
                                "enum": ["html", "txt", "md", "csv"],
                                "description": "附件格式，默认推荐 'html'（iPhone 点开即看精美排版卡片）",
                            },
                            "filename": {
                                "type": "string",
                                "description": "可选自定义文件名，如 'AI早报_1115.html'",
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
                    "description": "打开用户专属的真实浏览器执行网页交互任务（登录、查询动态页面、选座预订、填表等）。",
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
                                "description": "direct_notify 直接发提醒文案；agent_wakeup 到点唤醒小纳执行任务再汇报",
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

    async def _build_system_prompt(self, sender: str, query_hint: str) -> str:
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

        return (
            f"你是用户的贴身超级助理「小纳」（当前时间：{now_str}），通过手机 iMessage 与用户一对一沟通。\n\n"
            "【小纳交互铁律（必须严格遵守）】\n"
            "1. 真人贴身助理语气：自然、干脆、温暖、靠谱，像真人助理发短信一样说话，绝不端腔作势、不说机械套话。\n"
            "2. 小纳四原则：「结论先行，只留影响判断的部分，过程不写，重复合并」。严禁输出任何 Markdown 格式符号"
            "（严禁出现 **加粗**、### 标题、代码块 ```、Markdown 表格 |---|、[标题](链接)），"
            "也严禁向用户提 SQLite、FTS5、tool、headless、API、沙盒、函数调用 等任何底层技术黑话。\n"
            "3. 一屏短对话 + 重内容走附件卡片：每次气泡正文控制在手机一屏内（通常 3～8 行短句，适当空行）。"
            "如果用户要详细报告、长清单、多维对比或完整方案，先调用 `create_and_send_document(doc_format=\"html\")` "
            "把完整内容做成精美手机卡片附件发出去，气泡里只写 3 句核心结论！\n"
            "4. 链接必带且独立成行：凡涉及新闻、网页、店铺、车次航班或参考来源，必须在气泡末尾单独一行放上真实 "
            "https://... 链接（前后不要加括号或文字粘连），方便手机直接渲染原生预览卡片。\n"
            "5. 记忆与安全底线：用户的个人偏好、习惯与重要事实主动用 `save_memory` 记录，需回忆时用 `search_memory`；"
            "涉及付款、下单、删除、退订或对外发信等不可逆动作，必须先通过 `ask_human_confirmation` 向用户确认拍板，严禁编造实时数据。\n\n"
            f"【与当前对话相关的用户长期记忆】\n{mem_block}"
        )

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
                title = str(args.get("title", "")).strip() or "小纳整理卡片"
                content = str(args.get("content", ""))
                doc_format = str(args.get("doc_format", "html") or "html").strip().lower()
                if doc_format not in ("html", "txt", "md", "csv"):
                    doc_format = "html"
                raw_filename = str(args.get("filename", "")).strip()
                caption = str(args.get("caption", "")).strip()

                ts_tag = dt.datetime.now().strftime("%m%d_%H%M%S")
                stem = raw_filename if raw_filename else f"{title}_{ts_tag}"
                safe_name = sanitize_attachment_filename(
                    stem, default_stem=f"小纳卡片_{ts_tag}", ext=doc_format
                )
                deliverables_dir = self.config.state_dir / "deliverables"
                deliverables_dir.mkdir(parents=True, exist_ok=True)
                out_path = deliverables_dir / safe_name

                if doc_format == "html":
                    low_head = content.lstrip()[:64].lower()
                    if low_head.startswith("<!doctype html") or low_head.startswith("<html"):
                        rendered = content
                    else:
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

                await self.bridge.send_attachment(sender, out_path, caption=caption)
                return json.dumps(
                    {
                        "ok": True,
                        "filename": out_path.name,
                        "file_path": str(out_path),
                        "doc_format": doc_format,
                    },
                    ensure_ascii=False,
                )

            if fn_name == "send_local_attachment":
                raw_fp = str(args.get("file_path", "")).strip()
                caption = str(args.get("caption", "")).strip()
                if not raw_fp:
                    return json.dumps({"ok": False, "error": "file_path 不能为空"}, ensure_ascii=False)

                blocked, reason = is_sensitive_attachment_path(raw_fp)
                if blocked:
                    logger.warning(
                        "Blocked sensitive attachment send attempt by sender=%s path=%s (%s)",
                        sender,
                        raw_fp,
                        reason,
                    )
                    return json.dumps(
                        {"ok": False, "error": f"安全策略禁止发送敏感文件：{reason}"},
                        ensure_ascii=False,
                    )

                target_file = Path(raw_fp).expanduser().resolve()
                if not target_file.exists() or not target_file.is_file():
                    return json.dumps(
                        {"ok": False, "error": f"文件不存在或不是普通文件：{raw_fp}"},
                        ensure_ascii=False,
                    )

                await self.bridge.send_attachment(sender, target_file, caption=caption)
                return json.dumps(
                    {"ok": True, "sent_file": target_file.name},
                    ensure_ascii=False,
                )

            if fn_name == "run_browser_task":
                is_admin = self._is_admin_sender(sender)
                async with self._db_lock:
                    allowed_bq, bq_reason = self.store.check_and_increment_quota(
                        sender, kind="browser", is_admin=is_admin
                    )
                if not allowed_bq:
                    return json.dumps({"ok": False, "error": bq_reason}, ensure_ascii=False)

                # Suppress any redundant browser startup banner if instant ack was already sent!
                # Only schedule a quick browser ack if neither ack_sent nor an active ack_task exists.
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
                            await self.bridge.send_text(
                                sender,
                                "好嘞，我这就打开页面去办，中途如果要验证码或拍板我发消息喊你。",
                            )

                    local_browser_ack_task = asyncio.create_task(_delayed_browser_notice())

                prompt = str(args.get("task_prompt", "")).strip()
                max_steps = args.get("max_steps")
                try:
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
                    question=f"🔐 [需要你确认] {clean_q}\n直接回复验证码或你的决定就行：",
                    bridge=self.bridge,
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
    ) -> str:
        provider_name, api_key, base_url, is_deepseek_native = (
            self.config.resolve_active_provider()
        )
        if (
            provider_name == "unconfigured"
            and self._injected_llm_client is None
        ):
            return (
                "⚠️ 小纳还没有配置可用的模型密钥。\n"
                "请打开项目目录的 .env 文件，填入真实的 DEEPSEEK_API_KEY 或 OPENAI_API_KEY。"
            )

        if self._injected_llm_client is not None and provider_name == "unconfigured":
            is_deepseek_native = self.config.provider_mode == "deepseek"
            provider_name = "deepseek" if is_deepseek_native else "openai"

        model_name = (
            self.config.deepseek_model if is_deepseek_native else self.config.openai_model
        )
        client = self._get_or_create_client(api_key, base_url, is_fallback=False)

        sys_prompt = await self._build_system_prompt(sender, user_text)

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
        tools = self._build_tools(is_deepseek_native=is_deepseek_native)

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
                tools = self._build_tools(is_deepseek_native=False)
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

    async def handle_inbound_message(self, msg: InboundMessage) -> str | None:
        """Handle inbound user message: 2FA unlock -> auth gate -> live interruption -> instant ack + LLM turn."""
        # 1. Pending HumanGate 2FA resolution BEFORE locks/quotas/interruption to guarantee zero deadlocks
        if self.human_gate.try_resolve(msg.sender, msg.text):
            return None

        sender_norm = normalize_sender(msg.sender)
        is_admin = self._is_admin_sender(sender_norm)
        is_member = (not is_admin) and self.store.is_authorized_user(sender_norm)

        # 2. Multi-User Invite Beta authorization & self-service redemption gate
        if not is_admin and not is_member:
            if self.config.enable_invite_codes:
                redeemed, _ = self.store.redeem_invite_code(sender_norm, msg.text or "")
                if redeemed:
                    welcome = (
                        "🎉 邀请码激活成功！已为你分配独立记忆空间与专属浏览器沙盒，直接发消息即可开始使用。"
                    )
                    await self.bridge.send_text(msg.sender, welcome)
                    return welcome
                if self.store.should_send_invite_prompt(sender_norm):
                    prompt_msg = (
                        "👋 你好，当前 AI 助理处于邀请内测阶段，请直接回复邀请码完成激活。"
                    )
                    await self.bridge.send_text(msg.sender, prompt_msg)
                    return prompt_msg
            return None

        stripped = (msg.text or "").strip()
        if stripped.lower() in ("/ping", "ping"):
            reply = "🏓 小纳在线，随时吩咐。"
            await self.bridge.send_text(msg.sender, reply)
            return reply

        if stripped.lower() in ("/reset", "/clear"):
            async with self._db_lock:
                self.store.clear_conversation_history(msg.sender)
            reply = "🧹 当前对话记录已清空（你的长期记忆还留着）。"
            await self.bridge.send_text(msg.sender, reply)
            return reply

        # 3. Non-Blocking Live Interruption Routing ("插话分流": Stop / Status while a turn is active)
        active_info = self._active_turns.get(sender_norm)
        if active_info is not None and not active_info["task"].done():
            cmd_norm = _clean_command_phrase(stripped)
            if cmd_norm in _STOP_COMMANDS:
                active_info["cancelled_by_user"] = True
                active_info["task"].cancel()
                self._active_turns.pop(sender_norm, None)
                stop_reply = "好，刚才那件事已经停下了。有新安排你随时说。"
                await self.bridge.send_text(msg.sender, stop_reply)
                return stop_reply

            if cmd_norm in _STATUS_COMMANDS:
                elapsed = max(1, int(time.time() - float(active_info["started_at"])))
                summary = active_info["summary"]
                status_reply = (
                    f"还在办「{summary}」，已经跑了 {elapsed} 秒，"
                    "一弄完马上发给你（如果要停直接回「停」就行）。"
                )
                await self.bridge.send_text(msg.sender, status_reply)
                return status_reply

        # 4. Daily message quota enforcement
        async with self._db_lock:
            allowed_q, quota_reason = self.store.check_and_increment_quota(
                sender_norm, kind="msg", is_admin=is_admin
            )
        if not allowed_q:
            await self.bridge.send_text(msg.sender, quota_reason)
            return quota_reason

        # 5. Launch tracked turn task + Smart Delayed Ack Timer + Gentle Heartbeat
        has_images = bool(msg.image_data_urls)
        summary = _summarize_task_label(msg.text, has_images)
        ack_text = (
            self._craft_instant_ack(msg.text, has_images=has_images)
            if self.config.enable_instant_ack
            else ""
        )

        turn_ctx: dict[str, Any] = {
            "summary": summary,
            "started_at": time.time(),
            "ack_sent": False,
            "has_ack_timer": bool(ack_text),
            "cancelled_by_user": False,
        }

        turn_task: asyncio.Task[str] = asyncio.create_task(
            self._run_agent_turn(
                sender=msg.sender,
                user_text=msg.text,
                image_data_urls=msg.image_data_urls,
                turn_ctx=turn_ctx,
            )
        )
        turn_ctx["task"] = turn_task
        self._active_turns[sender_norm] = turn_ctx

        ack_task: asyncio.Task[None] | None = None
        if ack_text:
            is_heavy = _is_heavy_browser_intent(msg.text)
            if self.config.fast_ack_delay_sec <= 0:
                ack_delay = 0.0
            elif is_heavy:
                ack_delay = min(0.4, float(self.config.fast_ack_delay_sec))
            else:
                ack_delay = float(self.config.fast_ack_delay_sec)

            async def _ack_worker() -> None:
                if ack_delay > 0:
                    await asyncio.sleep(ack_delay)
                if (
                    not turn_task.done()
                    and not turn_ctx.get("ack_sent")
                    and not self.human_gate.has_pending(sender_norm)
                ):
                    turn_ctx["ack_sent"] = True
                    await self.bridge.send_text(msg.sender, ack_text)

            ack_task = asyncio.create_task(_ack_worker())

        heartbeat_task: asyncio.Task[None] | None = None
        hb_interval = float(self.config.heartbeat_interval_sec)
        if hb_interval > 0:

            async def _heartbeat_worker() -> None:
                while not turn_task.done():
                    await asyncio.sleep(hb_interval)
                    if turn_task.done():
                        break
                    if self.human_gate.has_pending(sender_norm):
                        continue
                    await self.bridge.send_text(
                        msg.sender,
                        f"还在处理「{summary}」，内容稍微有点多，弄完第一时间发你。",
                    )

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
            if self._active_turns.get(sender_norm) is turn_ctx:
                self._active_turns.pop(sender_norm, None)
            for helper_task in (ack_task, heartbeat_task):
                if helper_task is not None and not helper_task.done():
                    helper_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await helper_task

        formatted_reply = format_for_imessage(raw_reply)
        if formatted_reply:
            await self.bridge.send_text(msg.sender, formatted_reply)
        return formatted_reply

    async def process_due_tasks_once(self, now_ts: float | None = None) -> int:
        """Claim and execute all due scheduled tasks (direct_notify & agent_wakeup)."""
        async with self._db_lock:
            due_tasks = self.store.claim_due_tasks(now_ts=now_ts)
        for task in due_tasks:
            sender = task["sender"]
            task_type = task["task_type"]
            payload = task["payload"]
            tid = task["id"]
            try:
                if task_type == "direct_notify":
                    clean_payload = format_for_imessage(payload)
                    await self.bridge.send_text(sender, f"⏰ [定时提醒] {clean_payload}")
                elif task_type == "agent_wakeup":
                    wakeup_prompt = (
                        f"[系统定时唤醒任务 #{tid}] 请执行以下预定任务并按小纳简洁短信风格直接输出汇报：\n{payload}"
                    )
                    reply = await self._run_agent_turn(
                        sender=sender, user_text=wakeup_prompt
                    )
                    clean_reply = format_for_imessage(reply)
                    if clean_reply:
                        await self.bridge.send_text(sender, f"🔔 [定时汇报]\n{clean_reply}")
            except Exception:
                logger.exception("Failed executing scheduled task id=%s", tid)
        return len(due_tasks)


async def proactive_cron_loop(
    orchestrator: InstinctOrchestrator,
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
