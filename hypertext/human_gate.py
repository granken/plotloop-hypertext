"""Per-sender asynchronous Human-in-the-Loop (2FA / confirmation) gate."""

from __future__ import annotations

import asyncio
import logging
from itertools import count
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable

from .config import normalize_sender

if TYPE_CHECKING:
    from .bridge_imessage import MessageBridge

logger = logging.getLogger("hypertext.human_gate")


@dataclass
class PendingApproval:
    sender: str
    question: str
    future: asyncio.Future[str]
    token: int
    requester: asyncio.Task[object] | None


class HumanGate:
    """Manages per-sender asyncio.Future suspension for 2FA codes and approvals."""

    def __init__(self, default_timeout: float = 300.0) -> None:
        self.default_timeout = default_timeout
        self._pending: dict[str, PendingApproval] = {}
        self._tokens = count(1)
        self._closed = False

    def close(self) -> None:
        self._closed = True
        for entry in tuple(self._pending.values()):
            entry.future.cancel()
            if entry.requester is not None:
                entry.requester.cancel()
        self._pending.clear()

    def pending_token(self, senders: Iterable[str]) -> int | None:
        for sender in senders:
            entry = self._pending.get(normalize_sender(sender))
            if entry is not None and not entry.future.done():
                return entry.token
        return None

    def has_pending(self, sender: str) -> bool:
        key = normalize_sender(sender)
        entry = self._pending.get(key)
        return bool(entry is not None and not entry.future.done())

    def has_pending_any(self, senders: Iterable[str]) -> bool:
        return any(self.has_pending(s) for s in senders if s)

    def get_pending_question(self, sender: str) -> str | None:
        key = normalize_sender(sender)
        entry = self._pending.get(key)
        if entry is not None and not entry.future.done():
            return entry.question
        return None

    def try_resolve(self, sender: str, reply_text: str, *, token: int | None = None) -> bool:
        """If sender has a suspended 2FA/approval Future, resolve it immediately."""
        key = normalize_sender(sender)
        entry = self._pending.get(key)
        if entry is None or entry.future.done() or (token is not None and entry.token != token):
            return False
        answer = (reply_text or "").strip()
        entry.future.set_result(answer)
        self._pending.pop(key, None)
        logger.info("HumanGate resolved an approval")
        return True

    def try_resolve_any(self, senders: Iterable[str], reply_text: str, *, token: int | None = None) -> bool:
        """Try resolving pending Future across multiple aliases of the same user."""
        seen: set[str] = set()
        for s in senders:
            norm = normalize_sender(s)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            if self.try_resolve(norm, reply_text, token=token):
                return True
        return False

    async def request_input(
        self,
        sender: str,
        question: str,
        bridge: "MessageBridge | None" = None,
        timeout: float | None = None,
        reply_to: str | None = None,
    ) -> str:
        """Send prompt via bridge and suspend current task until sender replies."""
        if self._closed:
            raise asyncio.CancelledError
        key = normalize_sender(sender)
        outbound_target = (reply_to or "").strip() or sender
        effective_timeout = self.default_timeout if timeout is None else timeout
        loop = asyncio.get_running_loop()

        requester = asyncio.current_task()
        old = self._pending.pop(key, None)
        if old is not None and not old.future.done():
            old.future.cancel()
            if old.requester is not None and old.requester is not requester:
                old.requester.cancel()

        fut: asyncio.Future[str] = loop.create_future()
        self._pending[key] = PendingApproval(
            sender=key,
            question=question,
            future=fut,
            token=next(self._tokens),
            requester=requester,
        )

        try:
            try:
                async with asyncio.timeout(effective_timeout):
                    if bridge is not None:
                        await bridge.send_text(outbound_target, question)
                    return await fut
            finally:
                fut.cancel()
                current = self._pending.get(key)
                if current is not None and current.future is fut:
                    self._pending.pop(key, None)
        except asyncio.TimeoutError:
            logger.warning("HumanGate timed out after %.1fs", effective_timeout)
            if bridge is not None:
                await bridge.send_text(
                    outbound_target,
                    f"⏱️ 等待验证/确认回复已超时（{int(effective_timeout)}秒），本次敏感操作已安全中止。",
                )
            return "TIMEOUT: 用户未在规定时间内回复验证码或确认指令。"
