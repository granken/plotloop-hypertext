"""Classify inbound control commands and explicit replies to a live approval."""
from __future__ import annotations

import re


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
    "查进度",
    "什么进度",
    "到哪了",
    "到哪儿了",
    "进行到哪了",
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


def _clean_command_phrase(text: str) -> str:
    return re.sub(r"[\s!！?？.。,，~～、]+", "", (text or "").strip()).lower()


def _is_stop_command(text: str) -> bool:
    return _clean_command_phrase(text) in _STOP_COMMANDS


def _is_status_command(text: str) -> bool:
    return _clean_command_phrase(text) in _STATUS_COMMANDS


_APPROVAL_WORDS = {
    "确认", "同意", "继续", "允许", "批准", "可以", "是", "好", "好的", "确定",
    "拒绝", "不同意", "不可以", "不要", "否", "不批准",
    "yes", "y", "no", "n", "approve", "approved", "confirm", "confirmed", "deny",
    "denied", "reject", "rejected", "proceed",
}
_EXPLICIT_REPLY = re.compile(r"/reply\s+(.+)", re.IGNORECASE | re.DOTALL)
APPROVAL_REPLY_HINT = "直接回复验证码或「确认」「拒绝」。需要说明时，用 /reply 加上说明："


def is_explicit_approval_reply(text: str) -> bool:
    return _EXPLICIT_REPLY.fullmatch(text.strip()) is not None


def approval_answer(text: str) -> str | None:
    """Reserve short codes, clear decisions, and the explicit /reply command for HumanGate."""
    text = text.strip()
    explicit = _EXPLICIT_REPLY.fullmatch(text)
    if explicit:
        return explicit.group(1).strip()
    if _clean_command_phrase(text) in _APPROVAL_WORDS:
        return text
    code = re.sub(r"^(?:验证码|code|otp)[:：\s]+", "", text, flags=re.IGNORECASE)
    compact = re.sub(r"[ -]", "", code)
    if (
        re.fullmatch(r"[A-Za-z0-9]{4,10}", compact)
        and (any(ch.isdigit() for ch in compact) or compact.isupper())
    ):
        return compact
    return None
