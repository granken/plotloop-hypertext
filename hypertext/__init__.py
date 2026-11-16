"""HyperText (v0.5.3) — Your Ambient, Async AI Personal Agent over Apple Messages (iMessage)."""

from __future__ import annotations

__version__ = "0.5.3"

from .bridge_imessage import (
    APPLESCRIPT_SEND_ATTACHMENT,
    APPLESCRIPT_SEND_IMESSAGE,
    BOT_ECHO_MARKER,
    CliBridge,
    InboundMessage,
    MacIMessageBridge,
    MessageBridge,
    decode_attributed_body,
    encode_attributed_body,
)
from .cli import main, run_allow_user, run_doctor, run_revoke_user
from .config import AgentConfig, _is_placeholder_key, _load_dotenv, normalize_sender
from .formatter import (
    format_for_imessage,
    is_sensitive_attachment_path,
    render_mobile_html_card,
    sanitize_attachment_filename,
    split_reply_and_standalone_links,
)
from .human_gate import HumanGate
from .orchestrator import (
    HyperTextOrchestrator,
    InstinctOrchestrator,
    _simple_fetch_url,
    proactive_cron_loop,
)
from .sandbox_browser import BrowserSandbox
from .selftest import run_self_test
from .store_sqlite import StateStore, sanitize_conversation_history
from .xiaona_client import (
    XiaoNaClient,
    XiaoNaResult,
    extract_xiaona_query,
    format_xiaona_reply,
    is_compound_xiaona_request,
    is_explicit_xiaona_request,
    is_xiaomiguan_alias_used,
    is_xiaona_reset_command,
    normalize_xiaona_ask_url,
    normalize_xiaona_health_url,
)

__all__ = [
    "APPLESCRIPT_SEND_ATTACHMENT",
    "APPLESCRIPT_SEND_IMESSAGE",
    "__version__",
    "AgentConfig",
    "BOT_ECHO_MARKER",
    "BrowserSandbox",
    "CliBridge",
    "HumanGate",
    "HyperTextOrchestrator",
    "InboundMessage",
    "InstinctOrchestrator",
    "MacIMessageBridge",
    "MessageBridge",
    "StateStore",
    "XiaoNaClient",
    "XiaoNaResult",
    "_is_placeholder_key",
    "_load_dotenv",
    "_simple_fetch_url",
    "decode_attributed_body",
    "encode_attributed_body",
    "extract_xiaona_query",
    "format_for_imessage",
    "format_xiaona_reply",
    "is_compound_xiaona_request",
    "is_explicit_xiaona_request",
    "is_sensitive_attachment_path",
    "is_xiaomiguan_alias_used",
    "is_xiaona_reset_command",
    "main",
    "normalize_sender",
    "normalize_xiaona_ask_url",
    "normalize_xiaona_health_url",
    "proactive_cron_loop",
    "render_mobile_html_card",
    "run_allow_user",
    "run_doctor",
    "run_revoke_user",
    "run_self_test",
    "sanitize_attachment_filename",
    "sanitize_conversation_history",
    "split_reply_and_standalone_links",
]
