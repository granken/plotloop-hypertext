"""Instinct Mac mini Autonomous Personal Agent package."""

__version__ = "0.3.0"

from .bridge_imessage import (
    BOT_ECHO_MARKER,
    CliBridge,
    InboundMessage,
    MacIMessageBridge,
    MessageBridge,
    decode_attributed_body,
    encode_attributed_body,
)
from .config import AgentConfig, _is_placeholder_key, _load_dotenv, normalize_sender
from .human_gate import HumanGate
from .orchestrator import InstinctOrchestrator, _simple_fetch_url, proactive_cron_loop
from .sandbox_browser import BrowserSandbox
from .store_sqlite import StateStore, sanitize_conversation_history

__all__ = [
    "__version__",
    "AgentConfig",
    "_is_placeholder_key",
    "_load_dotenv",
    "normalize_sender",
    "BOT_ECHO_MARKER",
    "InboundMessage",
    "MessageBridge",
    "MacIMessageBridge",
    "CliBridge",
    "decode_attributed_body",
    "encode_attributed_body",
    "StateStore",
    "sanitize_conversation_history",
    "HumanGate",
    "BrowserSandbox",
    "InstinctOrchestrator",
    "_simple_fetch_url",
    "proactive_cron_loop",
]
