#!/usr/bin/env python3
"""Unified executable entrypoint and public re-export shim for HyperText (v0.5.2)."""

from __future__ import annotations

import sys

from hypertext import (
    BOT_ECHO_MARKER,
    AgentConfig,
    BrowserSandbox,
    CliBridge,
    HumanGate,
    HyperTextOrchestrator,
    InboundMessage,
    InstinctOrchestrator,
    MacIMessageBridge,
    MessageBridge,
    StateStore,
    XiaoNaClient,
    XiaoNaResult,
    __version__,
    _is_placeholder_key,
    _load_dotenv,
    _simple_fetch_url,
    decode_attributed_body,
    encode_attributed_body,
    extract_xiaona_query,
    format_xiaona_reply,
    is_compound_xiaona_request,
    is_explicit_xiaona_request,
    is_xiaona_reset_command,
    normalize_sender,
    normalize_xiaona_ask_url,
    normalize_xiaona_health_url,
    proactive_cron_loop,
    sanitize_conversation_history,
)
from hypertext.cli import (
    main,
    run_cli_repl,
    run_create_invite,
    run_doctor,
    run_imessage_daemon,
    run_list_users,
)
from hypertext.selftest import run_self_test

__all__ = [
    "__version__",
    "BOT_ECHO_MARKER",
    "AgentConfig",
    "_is_placeholder_key",
    "_load_dotenv",
    "normalize_sender",
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
    "XiaoNaClient",
    "XiaoNaResult",
    "extract_xiaona_query",
    "format_xiaona_reply",
    "is_compound_xiaona_request",
    "is_explicit_xiaona_request",
    "is_xiaona_reset_command",
    "normalize_xiaona_ask_url",
    "normalize_xiaona_health_url",
    "HyperTextOrchestrator",
    "InstinctOrchestrator",
    "_simple_fetch_url",
    "proactive_cron_loop",
    "run_self_test",
    "run_doctor",
    "run_create_invite",
    "run_list_users",
    "run_cli_repl",
    "run_imessage_daemon",
    "main",
]

if __name__ == "__main__":
    sys.exit(main())
