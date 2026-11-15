"""Browser-Use 1.0 compliant per-user isolated Chromium GUI sandbox with HumanGate 2FA actions."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import normalize_sender

if TYPE_CHECKING:
    from .bridge_imessage import MessageBridge
    from .config import AgentConfig
    from .human_gate import HumanGate

logger = logging.getLogger("instinct_mac.sandbox_browser")

_LOCAL_CHROMIUM_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)


class BrowserSandbox:
    """Executes stateful web tasks via Browser-Use 1.0 with per-user Chromium profile isolation."""

    def __init__(
        self,
        config: "AgentConfig",
        human_gate: "HumanGate",
        bridge: "MessageBridge | None" = None,
    ) -> None:
        self.config = config
        self.human_gate = human_gate
        self.bridge = bridge
        self._sender_locks: dict[str, asyncio.Lock] = {}
        self._concurrency_sem = asyncio.Semaphore(
            max(1, int(self.config.browser_max_concurrency))
        )

    @property
    def profile_dir(self) -> Path:
        """Backward-compatible default profile directory property."""
        return self.config.state_dir / "browser_profile"

    def profile_dir_for(self, sender: str) -> Path:
        """Return isolated per-user Chromium profile path with SHA-256 sender hash (zero PII)."""
        norm = normalize_sender(sender) or "anonymous"
        digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
        return self.config.state_dir / "browser_profiles" / f"user_{digest}"

    def _get_sender_lock(self, sender: str) -> asyncio.Lock:
        key = normalize_sender(sender) or "anonymous"
        lock = self._sender_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._sender_locks[key] = lock
        return lock

    def _detect_local_chromium(self) -> str:
        if self.config.chromium_executable_path:
            return self.config.chromium_executable_path
        for candidate in _LOCAL_CHROMIUM_CANDIDATES:
            if Path(candidate).exists():
                return candidate
        return "chromium"

    def _load_bu_classes(self) -> tuple[Any, Any, Any, Any] | None:
        os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
        try:
            from browser_use import (
                Agent as BrowserAgent,
                Browser,
                ChatOpenAI,
                Tools,
            )

            return (BrowserAgent, Browser, ChatOpenAI, Tools)
        except ImportError:
            return None

    async def run_task(
        self,
        sender: str,
        task_prompt: str,
        max_steps: int | None = None,
    ) -> str:
        """Run a Browser-Use 1.0 GUI task in the sender's isolated persistent Chromium profile."""
        loaded = self._load_bu_classes()
        if loaded is None:
            return (
                "浏览器自动化模块未就绪：当前环境未安装 `browser-use>=1.0.0`。"
                "请在项目 `.venv` 中安装依赖并初始化 Chromium。"
            )
        BrowserAgent, Browser, ChatOpenAI, Tools = loaded

        provider_name, api_key, base_url, is_ds = self.config.resolve_active_provider()
        if not api_key or provider_name == "unconfigured":
            return "浏览器任务无法启动：尚未配置有效的 `DEEPSEEK_API_KEY` 或 `OPENAI_API_KEY`。"

        if is_ds:
            model_name = self.config.deepseek_model
            bu_base_url = base_url.rstrip("/")
            if not bu_base_url.endswith("/v1"):
                bu_base_url = f"{bu_base_url}/v1"
        else:
            model_name = self.config.openai_model
            bu_base_url = base_url.rstrip("/")
            if not bu_base_url.endswith("/v1") and "openai.com" in bu_base_url:
                bu_base_url = f"{bu_base_url}/v1"

        llm_kwargs: dict[str, Any] = {
            "api_key": api_key,
            "base_url": bu_base_url,
        }
        if is_ds and self.config.deepseek_reasoning_effort != "none":
            llm_kwargs["reasoning_effort"] = self.config.deepseek_reasoning_effort
        try:
            llm = ChatOpenAI(model=model_name, **llm_kwargs)
        except TypeError:
            llm = ChatOpenAI(model_name, **llm_kwargs)

        tools = Tools()

        @tools.action(
            "Ask the human user via iMessage for a 2FA/OTP verification code "
            "or explicit approval before submitting an order, booking, payment, or account change."
        )
        async def ask_human_via_imessage(question: str) -> str:
            reply = await self.human_gate.request_input(
                sender=sender,
                question=f"🔐 [浏览器安全门禁] {question}\n请直接回复验证码或确认指令：",
                bridge=self.bridge,
            )
            return f"User response: {reply}"

        @tools.action(
            "Send a concise status update to the human user via iMessage during multi-step browser tasks"
        )
        async def notify_progress_via_imessage(status_update: str) -> str:
            if self.bridge is not None and status_update.strip():
                await self.bridge.send_text(
                    sender, f"🌐 [浏览器进度] {status_update.strip()}"
                )
            return "Status update sent."

        safety_rules = (
            "CRITICAL RULES:\n"
            "1. If the website requests a 2FA/MFA code, SMS/email OTP, or login verification, "
            "call `ask_human_via_imessage` immediately and wait for the code.\n"
            "2. Before clicking any final Pay, Purchase, Cancel Subscription, or Delete button, "
            "call `ask_human_via_imessage` with the exact item/price summary to get explicit approval.\n"
            "3. Reply concisely in the user's language when finished."
        )

        effective_steps = max(1, int(max_steps or self.config.browser_max_steps))
        user_profile_dir = self.profile_dir_for(sender)

        async with self._get_sender_lock(sender):
            async with self._concurrency_sem:
                user_profile_dir.mkdir(parents=True, exist_ok=True)
                try:
                    browser = Browser(
                        "chromium",
                        user_data_dir=str(user_profile_dir),
                        headless=self.config.browser_headless,
                    )
                except TypeError:
                    browser = Browser(
                        executable_path=self._detect_local_chromium(),
                        user_data_dir=str(user_profile_dir),
                        headless=self.config.browser_headless,
                    )

                agent_kwargs: dict[str, Any] = {
                    "task": task_prompt,
                    "llm": llm,
                    "browser": browser,
                    "tools": tools,
                    "extend_system_message": safety_rules,
                }
                if self.config.browser_sensitive_data:
                    agent_kwargs["sensitive_data"] = self.config.browser_sensitive_data

                bu_agent = BrowserAgent(**agent_kwargs)
                try:
                    history = await bu_agent.run(max_steps=effective_steps)
                    if hasattr(history, "final_result") and callable(history.final_result):
                        res = history.final_result()
                        return str(res) if res else "浏览器任务已完成。"
                    return str(history)
                finally:
                    for method_name in ("stop", "close"):
                        fn = getattr(browser, method_name, None)
                        if callable(fn):
                            maybe_coro = fn()
                            if asyncio.iscoroutine(maybe_coro):
                                await maybe_coro
                            break

    async def execute_web_errand(
        self,
        sender: str,
        task_prompt: str,
        max_steps: int | None = None,
    ) -> str:
        """Backward-compatible alias for run_task."""
        return await self.run_task(sender=sender, task_prompt=task_prompt, max_steps=max_steps)
