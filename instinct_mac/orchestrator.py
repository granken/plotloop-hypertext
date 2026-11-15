"""Instinct Orchestrator: DeepSeek V4.1-Flash native reasoning, multi-user invite/quotas, and OpenAI isolation."""

from __future__ import annotations

import asyncio
import datetime as dt
import html
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from typing import Any

from .bridge_imessage import InboundMessage, MessageBridge
from .config import AgentConfig, normalize_sender
from .human_gate import HumanGate
from .sandbox_browser import BrowserSandbox
from .store_sqlite import StateStore

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None  # type: ignore[assignment,misc]

logger = logging.getLogger("instinct_mac.orchestrator")


def _normalize_jsonable(obj: Any) -> Any:
    """Recursively convert SDK Pydantic objects or custom structs into plain JSON-serializable dicts/lists."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if hasattr(obj, "model_dump") and callable(obj.model_dump):
        return _normalize_jsonable(obj.model_dump())
    if isinstance(obj, dict):
        return {str(k): _normalize_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_normalize_jsonable(item) for item in obj]
    return str(obj)


def _fetch_url_sync(url: str, max_chars: int = 12000, timeout: float = 15.0) -> str:
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        return f"错误：仅支持 http/https 协议链接，已拒绝 `{parsed.scheme or 'unknown'}`。"

    req = urllib.request.Request(
        url.strip(),
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) InstinctMacAgent/0.3"
            ),
            "Accept": "text/html,application/xhtml+xml,text/plain,application/json;q=0.9,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_bytes = resp.read(1024 * 1024)
            charset = resp.headers.get_content_charset() or "utf-8"
            text = raw_bytes.decode(charset, errors="replace")
    except Exception as exc:
        return f"抓取网页失败 ({url}): {exc}"

    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    if len(text) > max_chars:
        return text[:max_chars] + "\n...[内容已截断]"
    return text or "(页面无可提取正文)"


async def _simple_fetch_url(url: str, max_chars: int = 12000, timeout: float = 15.0) -> str:
    """Local fallback webpage text fetcher used strictly in OpenAI-compatible mode."""
    return await asyncio.to_thread(_fetch_url_sync, url, max_chars, timeout)


class InstinctOrchestrator:
    """Cognitive routing, hybrid memory recall, multi-user invite/quotas, and proactive scheduling."""

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
        self._sender_locks: dict[str, asyncio.Lock] = {}

    def _is_admin_sender(self, sender: str) -> bool:
        norm = normalize_sender(sender)
        if not norm:
            return False
        if norm == "local-cli-user":
            return True
        if norm in self.config.allowed_senders:
            return True
        # Backward compatibility for embedded tests where allowed_senders was left empty and invite mode is off
        if not self.config.allowed_senders and not self.config.enable_invite_codes:
            return True
        return False

    def _get_sender_lock(self, sender: str) -> asyncio.Lock:
        key = normalize_sender(sender) or "default"
        lock = self._sender_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._sender_locks[key] = lock
        return lock

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

        cls_openai = AsyncOpenAI
        if cls_openai is None:
            try:
                from openai import AsyncOpenAI as _AsyncOpenAI

                cls_openai = _AsyncOpenAI
            except ImportError as exc:
                raise RuntimeError(
                    "未安装 `openai>=1.65.0` SDK，请在项目 `.venv` 虚拟环境中安装依赖后运行。"
                ) from exc

        client = cls_openai(api_key=api_key, base_url=base_url)
        self._client_cache[cache_key] = client
        return client

    async def _compute_optional_embedding(self, text: str) -> list[float] | None:
        """Compute optional dense embedding if EMBEDDING_MODEL is configured."""
        if not self.config.embedding_model or not text.strip():
            return None
        emb_key = self.config.embedding_api_key or self.config.openai_api_key
        emb_url = self.config.embedding_base_url or self.config.openai_base_url
        client = self._injected_embedding_client
        if client is None:
            if not emb_key:
                return None
            try:
                client = self._get_or_create_client(emb_key, emb_url, is_fallback=True)
            except Exception:
                return None
        try:
            resp = await client.embeddings.create(
                model=self.config.embedding_model,
                input=text.strip()[:4000],
            )
            vec = resp.data[0].embedding
            return [float(x) for x in vec]
        except Exception as exc:
            logger.debug("Optional embedding computation skipped: %s", exc)
            return None

    def _build_tools(self, is_deepseek_native: bool) -> list[dict[str, Any]]:
        """Build tool declarations obeying strict DeepSeek vs OpenAI isolation rules."""
        tools: list[dict[str, Any]] = [
            {
                "type": "function",
                "function": {
                    "name": "save_memory",
                    "description": "保存或更新用户的长期偏好、事实、习惯或SOP流程（持久化到本地 SQLite FTS5 记忆库）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "key": {
                                "type": "string",
                                "description": "简短唯一的英文或中文记忆键名，例如 coffee_pref、seat_habit",
                            },
                            "value": {
                                "type": "string",
                                "description": "详细的记忆内容描述",
                            },
                            "tags": {
                                "type": "string",
                                "description": "可选标签（逗号或空格分隔），便于混合检索",
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
                    "description": "通过中英文混合分词 FTS5 + BM25 + 精确加权搜索用户的历史长期记忆。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "搜索关键词（支持多词中文如 '虹桥 冰美式'、单字/双字如 '靠窗' 或英文前缀），留空返回最新记忆",
                            },
                            "limit": {
                                "type": "integer",
                                "description": "最大返回条数，默认 10",
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "delete_memory",
                    "description": "按 key 删除用户已过期或要求遗忘的某条长期记忆。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "key": {
                                "type": "string",
                                "description": "要删除的记忆键名 mem_key",
                            },
                        },
                        "required": ["key"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "run_browser_task",
                    "description": "调用该用户专属隔离的本地持久化 Chromium 浏览器沙箱执行需要登录态、点击、表单填写或网页交互的任务。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task_prompt": {
                                "type": "string",
                                "description": "清晰具体的浏览器执行指令（包含目标网址、步骤与期望提取结果）",
                            },
                            "max_steps": {
                                "type": "integer",
                                "description": "最大浏览器操作步数，默认 25",
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
                    "description": "在执行支付、下单、删除或需要短信验证码(2FA)的操作前，通过 iMessage 挂起并请求用户人工回复确认。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "向用户发送的具体确认问题或验证码提示",
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
                    "description": "创建定时主动任务：支持直接发送提醒(direct_notify)或定时唤醒智能体执行任务(agent_wakeup)。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task_type": {
                                "type": "string",
                                "enum": ["direct_notify", "agent_wakeup"],
                                "description": "direct_notify=直接发提醒文本；agent_wakeup=到点唤醒智能体联网/查记忆并汇报",
                            },
                            "payload": {
                                "type": "string",
                                "description": "提醒正文或唤醒后的任务描述",
                            },
                            "delay_seconds": {
                                "type": "number",
                                "description": "距离首次触发的秒数（例如 3600 表示 1 小时后）",
                            },
                            "interval_seconds": {
                                "type": "number",
                                "description": "周期重复秒数，0 表示仅执行一次",
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
                        "description": "抓取指定 http/https 网页的正文文本内容（OpenAI 兼容模式本地工具）。",
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
            f"你是运行在 Mac mini 上的全自主个人智能体 Instinct（当前时间：{now_str}）。\n"
            "交互通道为 iMessage，请保持回复简洁、直接、高信息密度，适合手机阅读。\n"
            "核心准则：\n"
            "1. 用户的个人偏好、习惯、重要事实请主动用 `save_memory` 记录；需要回忆时用 `search_memory` 检索；用户要求删除或修正旧信息时用 `delete_memory`。\n"
            "2. 涉及支付、下单、不可逆修改或需要短信验证码(2FA)时，必须通过 `ask_human_confirmation` 或浏览器安全门禁征求用户确认，绝不擅自提交。\n"
            "3. 严禁编造实时信息或未确认的数字。\n\n"
            f"【与当前对话相关的用户长期记忆】\n{mem_block}"
        )

    async def _execute_local_tool(
        self, sender: str, fn_name: str, raw_args: str
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
                self.store.save_memory(sender, key, value, tags=tags, embedding=emb)
                return json.dumps({"ok": True, "saved_key": key}, ensure_ascii=False)

            if fn_name == "search_memory":
                query = str(args.get("query", "")).strip()
                limit = int(args.get("limit", 10) or 10)
                q_emb = await self._compute_optional_embedding(query) if query else None
                hits = self.store.search_memories(
                    sender, query=query, limit=limit, query_embedding=q_emb
                )
                return json.dumps({"ok": True, "count": len(hits), "memories": hits}, ensure_ascii=False)

            if fn_name == "delete_memory":
                key = str(args.get("key", "")).strip()
                if not key:
                    return json.dumps({"ok": False, "error": "key 不能为空"}, ensure_ascii=False)
                deleted = self.store.delete_memory(sender, key)
                return json.dumps({"ok": True, "deleted": deleted, "key": key}, ensure_ascii=False)

            if fn_name == "run_browser_task":
                is_admin = self._is_admin_sender(sender)
                allowed_bq, bq_reason = self.store.check_and_increment_quota(
                    sender, kind="browser", is_admin=is_admin
                )
                if not allowed_bq:
                    return json.dumps({"ok": False, "error": bq_reason}, ensure_ascii=False)

                prompt = str(args.get("task_prompt", "")).strip()
                max_steps = args.get("max_steps")
                result = await self.browser_sandbox.run_task(
                    sender=sender,
                    task_prompt=prompt,
                    max_steps=int(max_steps) if max_steps else None,
                )
                return json.dumps({"ok": True, "result": result}, ensure_ascii=False)

            if fn_name == "ask_human_confirmation":
                question = str(args.get("question", "")).strip() or "请确认是否继续执行敏感操作？"
                answer = await self.human_gate.request_input(
                    sender=sender,
                    question=f"🔐 [安全确认] {question}\n请直接回复验证码或确认意见：",
                    bridge=self.bridge,
                )
                return json.dumps({"ok": True, "user_reply": answer}, ensure_ascii=False)

            if fn_name == "schedule_task":
                task_type = str(args.get("task_type", "direct_notify")).strip()
                payload = str(args.get("payload", "")).strip()
                delay_sec = max(0.0, float(args.get("delay_seconds", 60.0)))
                interval_sec = max(0.0, float(args.get("interval_seconds", 0.0) or 0.0))
                run_at = time.time() + delay_sec
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
                tasks = self.store.list_scheduled_tasks(sender=sender, only_enabled=True)
                return json.dumps({"ok": True, "tasks": tasks}, ensure_ascii=False)

            if fn_name == "cancel_scheduled_task":
                tid = int(args.get("task_id", 0))
                cancelled = self.store.cancel_scheduled_task(tid, sender=sender)
                return json.dumps({"ok": True, "cancelled": cancelled, "task_id": tid}, ensure_ascii=False)

            if fn_name == "fetch_url_text":
                url = str(args.get("url", "")).strip()
                content = await _simple_fetch_url(url)
                return json.dumps({"ok": True, "url": url, "content": content}, ensure_ascii=False)

            return json.dumps({"ok": False, "error": f"未知工具: {fn_name}"}, ensure_ascii=False)
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

        model_extra = getattr(choice_message, "model_extra", None)
        if not isinstance(model_extra, dict):
            model_extra = {}

        raw_reasoning = getattr(choice_message, "reasoning_content", None)
        if raw_reasoning is None:
            raw_reasoning = model_extra.get("reasoning_content")
        reasoning_str = raw_reasoning if isinstance(raw_reasoning, str) else ""

        raw_stc = getattr(choice_message, "server_tool_calls", None)
        if raw_stc is None:
            raw_stc = model_extra.get("server_tool_calls")
        server_tool_calls = _normalize_jsonable(raw_stc) if raw_stc else None
        if server_tool_calls is not None and not isinstance(server_tool_calls, list):
            server_tool_calls = [server_tool_calls]

        raw_tcs = getattr(choice_message, "tool_calls", None)
        norm_tcs: list[dict[str, Any]] | None = None
        if raw_tcs:
            norm_tcs = []
            for idx, tc in enumerate(raw_tcs):
                if isinstance(tc, dict):
                    tc_id = str(tc.get("id") or f"call_{idx}")
                    fn_dict = tc.get("function") or {}
                    fn_name = str(fn_dict.get("name") or "")
                    fn_args = str(fn_dict.get("arguments") or "{}")
                else:
                    tc_id = str(getattr(tc, "id", None) or f"call_{idx}")
                    fn_obj = getattr(tc, "function", None)
                    fn_name = str(getattr(fn_obj, "name", "") or "")
                    fn_args = str(getattr(fn_obj, "arguments", "{}") or "{}")
                norm_tcs.append(
                    {
                        "id": tc_id,
                        "type": "function",
                        "function": {"name": fn_name, "arguments": fn_args},
                    }
                )

        return (content_str, reasoning_str, norm_tcs, server_tool_calls)

    async def _run_agent_turn(
        self,
        sender: str,
        user_text: str,
        image_data_urls: list[str] | None = None,
    ) -> str:
        provider_name, api_key, base_url, is_deepseek_native = (
            self.config.resolve_active_provider()
        )
        if (
            provider_name == "unconfigured"
            and self._injected_llm_client is None
        ):
            return (
                "⚠️ Instinct 尚未配置有效的模型 API 密钥。\n"
                "请编辑项目 `.env` 文件，将 `DEEPSEEK_API_KEY`（推荐）或 `OPENAI_API_KEY` 替换为真实密钥。"
            )

        if self._injected_llm_client is not None and provider_name == "unconfigured":
            is_deepseek_native = self.config.provider_mode == "deepseek"
            provider_name = "deepseek" if is_deepseek_native else "openai"

        model_name = (
            self.config.deepseek_model if is_deepseek_native else self.config.openai_model
        )
        client = self._get_or_create_client(api_key, base_url, is_fallback=False)

        sys_prompt = await self._build_system_prompt(sender, user_text)
        history = self.store.get_conversation_history(
            sender=sender,
            limit=self.config.max_history_messages,
            is_deepseek_native=is_deepseek_native,
        )

        if image_data_urls:
            user_content: Any = [{"type": "text", "text": user_text}] + [
                {"type": "image_url", "image_url": {"url": u}} for u in image_data_urls
            ]
        else:
            user_content = user_text

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
                    return f"⚠️ 模型调用失败：{primary_exc}"

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
                return content_str.strip() or "（已完成处理）"

            for tc in tool_calls:
                tc_id = tc["id"]
                fn_name = tc["function"]["name"]
                fn_args = tc["function"]["arguments"]
                tool_output = await self._execute_local_tool(sender, fn_name, fn_args)
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

        return "⚠️ 已达到单轮最大工具调用步数上限，请简化任务或分步执行。"

    async def handle_inbound_message(self, msg: InboundMessage) -> str | None:
        """Handle inbound user message: 2FA unlock -> invite gate -> quota check -> LLM turn."""
        # 1. Pending HumanGate 2FA resolution BEFORE locks/quotas to guarantee zero deadlocks
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
            reply = "🏓 Instinct 在线运行中。"
            await self.bridge.send_text(msg.sender, reply)
            return reply

        if stripped.lower() in ("/reset", "/clear"):
            async with self._get_sender_lock(msg.sender):
                self.store.clear_conversation_history(msg.sender)
            reply = "🧹 当前会话上下文已清空（长期记忆仍保留）。"
            await self.bridge.send_text(msg.sender, reply)
            return reply

        # 3. Daily message quota enforcement
        allowed_q, quota_reason = self.store.check_and_increment_quota(
            sender_norm, kind="msg", is_admin=is_admin
        )
        if not allowed_q:
            await self.bridge.send_text(msg.sender, quota_reason)
            return quota_reason

        async with self._get_sender_lock(msg.sender):
            reply = await self._run_agent_turn(
                sender=msg.sender,
                user_text=msg.text,
                image_data_urls=msg.image_data_urls,
            )
            if reply:
                await self.bridge.send_text(msg.sender, reply)
            return reply

    async def process_due_tasks_once(self, now_ts: float | None = None) -> int:
        """Claim and execute all due scheduled tasks (direct_notify & agent_wakeup)."""
        due_tasks = self.store.claim_due_tasks(now_ts=now_ts)
        for task in due_tasks:
            sender = task["sender"]
            task_type = task["task_type"]
            payload = task["payload"]
            tid = task["id"]
            try:
                if task_type == "direct_notify":
                    await self.bridge.send_text(sender, f"⏰ [定时提醒] {payload}")
                elif task_type == "agent_wakeup":
                    async with self._get_sender_lock(sender):
                        wakeup_prompt = (
                            f"[系统定时唤醒任务 #{tid}] 请执行以下预定任务并直接输出发给用户的汇报：\n{payload}"
                        )
                        reply = await self._run_agent_turn(
                            sender=sender, user_text=wakeup_prompt
                        )
                        if reply:
                            await self.bridge.send_text(sender, f"🔔 [主动任务汇报]\n{reply}")
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
