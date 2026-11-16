"""Multi-Harness Local Agent Matrix (v0.6.0): Explicit-Only Owner-Gated DSH, Codex & AGY CLI Clients."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import re
import shutil
import socket
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping, Sequence

from .config import (
    AgentConfig,
    _parse_dotenv_text,
    _resolve_dotenv_candidates,
    normalize_sender,
)
from .dreamina_client import is_explicit_dreamina_request
from .formatter import format_for_imessage
from .workbuddy_client import (
    SubprocessRunnerCallable,
    _default_subprocess_runner,
    _extract_from_parsed_json,
    _is_executable_file,
    _parse_node_version_tuple,
    collect_changed_deliverables,
    is_explicit_workbuddy_request,
    snapshot_workspace_files,
)
from .xiaona_client import is_explicit_xiaona_request

if TYPE_CHECKING:
    from .store_sqlite import StateStore

logger = logging.getLogger("hypertext.harnesses_client")

MIN_HARNESS_TIMEOUT_SEC = 600.0
DEFAULT_LOCAL_PROXY_HOST = "127.0.0.1"
DEFAULT_LOCAL_PROXY_PORT = 7897
DEFAULT_MAGPIE_HOST = "127.0.0.1"
DEFAULT_MAGPIE_PORT = 3425

DEFAULT_DSH_MAGPIE_MODEL = "auto/deepseek-v4.1-flash"
DEFAULT_CODEX_MODEL_CHAIN: tuple[str, ...] = (
    "gpt-6-luna",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-6-sol",
)
DEFAULT_AGY_MODEL = "gemini-4-argon-high"
DEFAULT_AGY_MODEL_CHAIN: tuple[str, ...] = (
    "gemini-4-argon-high",
    "gemini-4-argon-medium",
    "gemini-4-carbon-high",
)

_URL_STRIP_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_DELEGATION_VERBS_PAT = (
    r"(?:问一下|问下|问问|请问|问|让|请|叫|找|喊|麻烦|呼叫|唤醒|转给|交给|发给|委托|调用|用)"
)

_LOCAL_MAC_COMPOUND_RE = re.compile(
    r"(?:"
    r"定个闹钟|设个闹钟|定闹钟|设闹钟"
    r"|设个提醒|定个提醒|设提醒|定提醒|加个提醒|创建提醒|定时提醒"
    r"|分钟后提醒|小时后提醒|点提醒我|点半提醒我|提醒我一下"
    r"|用本地浏览器|打开本地浏览器|打开本地网页|在本机打开|截屏|截图"
    r")"
)

# --- DSH Patterns ---
_DSH_TOKEN_PAT = r"(?<![A-Za-z0-9_./-])(?i:dsh|deepseek\s+harness)(?![A-Za-z0-9_])"
_DSH_LEAD_RE = re.compile(
    r"^[/／@]\s*(?i:dsh)(?![A-Za-z0-9_])(?:$|\s+|[:：,，!！?？.。、~～\-]|(?=[\u4e00-\u9fff]))"
)
_DSH_WORD_RE = re.compile(_DSH_TOKEN_PAT)
_DSH_RESET_RE = re.compile(
    rf"^(?:"
    rf"[/／@]\s*(?i:dsh)(?![A-Za-z0-9_])\s*(?:[:：]\s*)?(?:reset|clear|重置|清空|重置会话|清空会话|重置上下文|清空上下文)"
    rf"|{_DSH_TOKEN_PAT}\s*(?:[:：]\s*)?(?:reset|clear)"
    rf"|(?:重置|清空)\s*{_DSH_TOKEN_PAT}\s*(?:的)?(?:会话|上下文|对话)?"
    rf")$",
    re.IGNORECASE,
)
_STRIP_DSH_LEAD_RE = re.compile(r"^[/／@]\s*(?i:dsh)(?![A-Za-z0-9_])[\s:：,，、\-]*")
_STRIP_DSH_DELEGATION_RE = re.compile(
    rf"^(?:帮我|麻烦你|麻烦|请你)?\s*{_DELEGATION_VERBS_PAT}\s*{_DSH_TOKEN_PAT}"
    rf"(?:\s*(?:回答一下|回答下|回答|说一下|说下|处理一下|处理下|跑一下|跑下))?[\s:：,，、]*"
)
_STRIP_DSH_VOCATIVE_RE = re.compile(
    rf"^(?:嘿|嗨|喂|你好|哈喽)?[\s,，!！]*{_DSH_TOKEN_PAT}(?:\s*[:：,，!！、~～]+|\s+|$)"
)

# --- Codex Patterns ---
_CODEX_FULL_PAT = r"(?<![A-Za-z0-9_./-])(?i:codex)(?![A-Za-z0-9_])"
_CODEX_LEAD_PAT = r"(?i:codex|cdx)(?![A-Za-z0-9_])"
_CODEX_LEAD_RE = re.compile(
    rf"^[/／@]\s*{_CODEX_LEAD_PAT}(?:$|\s+|[:：,，!！?？.。、~～\-]|(?=[\u4e00-\u9fff]))"
)
_CODEX_WORD_RE = re.compile(_CODEX_FULL_PAT)
_CDX_DELEGATION_RE = re.compile(
    rf"{_DELEGATION_VERBS_PAT}\s*(?<![A-Za-z0-9_./-])(?i:cdx)(?![A-Za-z0-9_])"
)
_CODEX_RESET_RE = re.compile(
    rf"^(?:"
    rf"[/／@]\s*{_CODEX_LEAD_PAT}\s*(?:[:：]\s*)?(?:reset|clear|重置|清空|重置会话|清空会话|重置上下文|清空上下文)"
    rf"|(?<![A-Za-z0-9_./-])(?i:codex|cdx)(?![A-Za-z0-9_])\s*(?:[:：]\s*)?(?:reset|clear)"
    rf"|(?:重置|清空)\s*(?<![A-Za-z0-9_./-])(?i:codex|cdx)(?![A-Za-z0-9_])\s*(?:的)?(?:会话|上下文|对话)?"
    rf")$",
    re.IGNORECASE,
)
_STRIP_CODEX_LEAD_RE = re.compile(rf"^[/／@]\s*{_CODEX_LEAD_PAT}[\s:：,，、\-]*")
_STRIP_CODEX_DELEGATION_RE = re.compile(
    rf"^(?:帮我|麻烦你|麻烦|请你)?\s*{_DELEGATION_VERBS_PAT}\s*"
    rf"(?<![A-Za-z0-9_./-])(?i:codex|cdx)(?![A-Za-z0-9_])"
    rf"(?:\s*(?:回答一下|回答下|回答|说一下|说下|处理一下|处理下|跑一下|跑下))?[\s:：,，、]*"
)
_STRIP_CODEX_VOCATIVE_RE = re.compile(
    rf"^(?:嘿|嗨|喂|你好|哈喽)?[\s,，!！]*{_CODEX_FULL_PAT}(?:\s*[:：,，!！、~～]+|\s+|$)"
)

# --- AGY Patterns ---
_AGY_TOKEN_PAT = r"(?<![A-Za-z0-9_./-])(?i:agy|antigravity)(?![A-Za-z0-9_])"
_AGY_LEAD_RE = re.compile(
    r"^[/／@]\s*(?i:agy|antigravity)(?![A-Za-z0-9_])(?:$|\s+|[:：,，!！?？.。、~～\-]|(?=[\u4e00-\u9fff]))"
)
_AGY_WORD_RE = re.compile(_AGY_TOKEN_PAT)
_AGY_RESET_RE = re.compile(
    rf"^(?:"
    rf"[/／@]\s*(?i:agy|antigravity)(?![A-Za-z0-9_])\s*(?:[:：]\s*)?(?:reset|clear|重置|清空|重置会话|清空会话|重置上下文|清空上下文)"
    rf"|{_AGY_TOKEN_PAT}\s*(?:[:：]\s*)?(?:reset|clear)"
    rf"|(?:重置|清空)\s*{_AGY_TOKEN_PAT}\s*(?:的)?(?:会话|上下文|对话)?"
    rf")$",
    re.IGNORECASE,
)
_STRIP_AGY_LEAD_RE = re.compile(r"^[/／@]\s*(?i:agy|antigravity)(?![A-Za-z0-9_])[\s:：,，、\-]*")
_STRIP_AGY_DELEGATION_RE = re.compile(
    rf"^(?:帮我|麻烦你|麻烦|请你)?\s*{_DELEGATION_VERBS_PAT}\s*{_AGY_TOKEN_PAT}"
    rf"(?:\s*(?:回答一下|回答下|回答|说一下|说下|处理一下|处理下|跑一下|跑下))?[\s:：,，、]*"
)
_STRIP_AGY_VOCATIVE_RE = re.compile(
    rf"^(?:嘿|嗨|喂|你好|哈喽)?[\s,，!！]*{_AGY_TOKEN_PAT}(?:\s*[:：,，!！、~～]+|\s+|$)"
)


def _clean_inbound(text: str) -> str:
    return (text or "").replace("\u2063", "").replace("\ufffc", "").replace("\x00", "").strip()


def _strip_urls(text: str) -> str:
    return _URL_STRIP_RE.sub(" ", text or "")


def _probe_tcp_port(host: str, port: int, timeout_sec: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_sec):
            return True
    except OSError:
        return False


def _read_live_dotenv(
    config: AgentConfig, allow_repo_dotenv_fallback: bool = True
) -> dict[str, str]:
    paths: list[Path]
    if config.loaded_env_file is not None:
        paths = [Path(config.loaded_env_file).expanduser()]
    elif allow_repo_dotenv_fallback:
        paths = _resolve_dotenv_candidates(None)
    else:
        paths = []

    merged: dict[str, str] = {}
    for cand in paths:
        try:
            if not cand.is_file():
                continue
            parsed = _parse_dotenv_text(cand.read_text(encoding="utf-8"))
            for k, v in parsed.items():
                merged.setdefault(k, v)
        except OSError:
            continue
    return merged


def ensure_localhost_no_proxy(env: dict[str, str]) -> dict[str, str]:
    """Guarantee `127.0.0.1`, `localhost`, and `::1` are present in both `NO_PROXY` and `no_proxy`."""
    existing_raw = f"{env.get('NO_PROXY', '')},{env.get('no_proxy', '')}"
    items: list[str] = []
    seen: set[str] = set()
    for part in existing_raw.split(","):
        tok = part.strip()
        if tok and tok.lower() not in seen:
            seen.add(tok.lower())
            items.append(tok)
    for req_host in ("127.0.0.1", "localhost", "::1"):
        if req_host.lower() not in seen:
            seen.add(req_host.lower())
            items.append(req_host)
    joined = ",".join(items)
    env["NO_PROXY"] = joined
    env["no_proxy"] = joined
    return env


def apply_local_proxy_env(
    env: dict[str, str],
    config: AgentConfig,
    proxy_probe: Callable[[str, int], bool] | None = None,
    *,
    allow_repo_dotenv_fallback: bool = True,
) -> dict[str, str]:
    """Configure `HTTP(S)_PROXY` for Codex/AGY while keeping `127.0.0.1,localhost,::1` in `NO_PROXY`."""
    ensure_localhost_no_proxy(env)
    if (env.get("HTTPS_PROXY") or "").strip() or (env.get("https_proxy") or "").strip():
        return env

    live_vars = _read_live_dotenv(
        config, allow_repo_dotenv_fallback=allow_repo_dotenv_fallback
    )
    live_proxy = (live_vars.get("HYPERTEXT_LOCAL_PROXY") or "").strip()
    mem_proxy = (getattr(config, "local_http_proxy", "") or "").strip()
    explicit_proxy = (
        (live_proxy or mem_proxy)
        if config.loaded_env_file is not None
        else (mem_proxy or live_proxy)
    )

    chosen_proxy = explicit_proxy
    if not chosen_proxy:
        probe_fn = proxy_probe or _probe_tcp_port
        if probe_fn(DEFAULT_LOCAL_PROXY_HOST, DEFAULT_LOCAL_PROXY_PORT):
            chosen_proxy = f"http://{DEFAULT_LOCAL_PROXY_HOST}:{DEFAULT_LOCAL_PROXY_PORT}"

    if chosen_proxy:
        env["http_proxy"] = chosen_proxy
        env["https_proxy"] = chosen_proxy
        env["HTTP_PROXY"] = chosen_proxy
        env["HTTPS_PROXY"] = chosen_proxy
    return env


def _prepend_lexical_dirs_to_path(
    env: dict[str, str], lexical_dirs: Sequence[str]
) -> dict[str, str]:
    cur_path = env.get("PATH", "")
    parts = [p for p in cur_path.split(os.pathsep) if p] if cur_path else []
    for d in reversed(lexical_dirs):
        if not d:
            continue
        if d in parts:
            parts.remove(d)
        parts.insert(0, d)
    env["PATH"] = os.pathsep.join(parts)
    return env


# ============================================================================
# Explicit-Only Word-Boundary & URL-Safe Trigger Parsers
# ============================================================================


def is_dsh_reset_command(text: str) -> bool:
    cleaned = re.sub(r"[!！?？.。~～\s]+$", "", _clean_inbound(text)).strip()
    return bool(cleaned and _DSH_RESET_RE.fullmatch(cleaned))


def is_explicit_dsh_request(text: str) -> bool:
    cleaned = _clean_inbound(text)
    if not cleaned:
        return False
    if is_dsh_reset_command(cleaned) or _DSH_LEAD_RE.search(cleaned):
        return True
    return bool(_DSH_WORD_RE.search(_strip_urls(cleaned)))


def extract_dsh_query(text: str) -> str:
    cleaned = _clean_inbound(text)
    if not cleaned:
        return ""
    for pat in (_STRIP_DSH_LEAD_RE, _STRIP_DSH_DELEGATION_RE, _STRIP_DSH_VOCATIVE_RE):
        updated, count = pat.subn("", cleaned, count=1)
        if count > 0:
            return updated.strip()
    return cleaned


def is_codex_reset_command(text: str) -> bool:
    cleaned = re.sub(r"[!！?？.。~～\s]+$", "", _clean_inbound(text)).strip()
    return bool(cleaned and _CODEX_RESET_RE.fullmatch(cleaned))


def is_explicit_codex_request(text: str) -> bool:
    cleaned = _clean_inbound(text)
    if not cleaned:
        return False
    if is_codex_reset_command(cleaned) or _CODEX_LEAD_RE.search(cleaned):
        return True
    no_url = _strip_urls(cleaned)
    return bool(_CODEX_WORD_RE.search(no_url) or _CDX_DELEGATION_RE.search(no_url))


def extract_codex_query(text: str) -> str:
    cleaned = _clean_inbound(text)
    if not cleaned:
        return ""
    for pat in (_STRIP_CODEX_LEAD_RE, _STRIP_CODEX_DELEGATION_RE, _STRIP_CODEX_VOCATIVE_RE):
        updated, count = pat.subn("", cleaned, count=1)
        if count > 0:
            return updated.strip()
    return cleaned


def is_agy_reset_command(text: str) -> bool:
    cleaned = re.sub(r"[!！?？.。~～\s]+$", "", _clean_inbound(text)).strip()
    return bool(cleaned and _AGY_RESET_RE.fullmatch(cleaned))


def is_explicit_agy_request(text: str) -> bool:
    cleaned = _clean_inbound(text)
    if not cleaned:
        return False
    if is_agy_reset_command(cleaned) or _AGY_LEAD_RE.search(cleaned):
        return True
    return bool(_AGY_WORD_RE.search(_strip_urls(cleaned)))


def extract_agy_query(text: str) -> str:
    cleaned = _clean_inbound(text)
    if not cleaned:
        return ""
    for pat in (_STRIP_AGY_LEAD_RE, _STRIP_AGY_DELEGATION_RE, _STRIP_AGY_VOCATIVE_RE):
        updated, count = pat.subn("", cleaned, count=1)
        if count > 0:
            return updated.strip()
    return cleaned


def is_local_mac_compound_action(text: str) -> bool:
    """Return True if user text also requests local Mac reminders/alarms or local Browser-Use."""
    return bool(_LOCAL_MAC_COMPOUND_RE.search(_clean_inbound(text)))


_CHANNEL_FIND_PATTERNS: dict[str, re.Pattern[str]] = {
    "xiaona": re.compile(r"(?:小蜜罐|小纳|(?<![A-Za-z0-9_])(?i:xiaona|xiaomiguan)(?![A-Za-z0-9_]))"),
    "workbuddy": re.compile(
        r"(?:^[/／@]\s*(?i:wb)(?![A-Za-z0-9_])|(?<![A-Za-z0-9_])(?i:workbuddy|codebuddy)(?![A-Za-z0-9_]))"
    ),
    "dsh": re.compile(
        r"(?:^[/／@]\s*(?i:dsh)(?![A-Za-z0-9_])|(?<![A-Za-z0-9_./-])(?i:dsh|deepseek\s+harness)(?![A-Za-z0-9_]))"
    ),
    "codex": re.compile(
        r"(?:^[/／@]\s*(?i:codex|cdx)(?![A-Za-z0-9_])|(?<![A-Za-z0-9_./-])(?i:codex|cdx)(?![A-Za-z0-9_]))"
    ),
    "agy": re.compile(
        r"(?:^[/／@]\s*(?i:agy|antigravity)(?![A-Za-z0-9_])|(?<![A-Za-z0-9_./-])(?i:agy|antigravity)(?![A-Za-z0-9_]))"
    ),
    "dreamina": re.compile(
        r"(?:^[/／@]\s*(?:即梦(?:\s*画布)?(?![A-Za-z0-9_])|(?i:dreamina(?:-canvas)?|jimeng|jm)(?![A-Za-z0-9_]))"
        r"|即梦(?:\s*画布)?(?![A-Za-z0-9_])"
        r"|(?<![A-Za-z0-9_./-])(?i:dreamina(?:-canvas)?|jimeng|jm)(?![A-Za-z0-9_]))"
    ),
}


def detect_explicit_channels(text: str) -> list[str]:
    """Return ordered list of explicitly invoked specialist channels in `text`."""
    cleaned = _clean_inbound(text)
    if not cleaned:
        return []

    checks = [
        ("xiaona", is_explicit_xiaona_request(cleaned)),
        ("workbuddy", is_explicit_workbuddy_request(cleaned)),
        ("dsh", is_explicit_dsh_request(cleaned)),
        ("codex", is_explicit_codex_request(cleaned)),
        ("agy", is_explicit_agy_request(cleaned)),
        ("dreamina", is_explicit_dreamina_request(cleaned)),
    ]
    no_url = _strip_urls(cleaned)
    matched: list[tuple[int, int, str]] = []
    for order_idx, (ch_name, active) in enumerate(checks):
        if not active:
            continue
        pat = _CHANNEL_FIND_PATTERNS[ch_name]
        m = pat.search(no_url) or pat.search(cleaned)
        pos = m.start() if m else (10_000 + order_idx)
        matched.append((pos, order_idx, ch_name))

    matched.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in matched]


# ============================================================================
# CLI Binary Discovery (Zero Hardcoded Home Paths)
# ============================================================================


def discover_dsh_cli(
    config: AgentConfig,
    *,
    which_fn: Callable[[str], str | None] = shutil.which,
    static_candidates: Sequence[str | Path] | None = None,
    allow_repo_dotenv_fallback: bool = True,
) -> str:
    """Locate `dsh` binary dynamically without resolving symlinks."""
    live_vars = _read_live_dotenv(config, allow_repo_dotenv_fallback=allow_repo_dotenv_fallback)
    live_p = (live_vars.get("DSH_CLI_PATH") or "").strip()
    mem_p = (getattr(config, "dsh_cli_path", "") or "").strip()
    for raw_c in ((live_p, mem_p) if config.loaded_env_file is not None else (mem_p, live_p)):
        if raw_c:
            exp = os.path.abspath(os.path.expanduser(raw_c))
            if _is_executable_file(exp):
                return exp

    found = which_fn("dsh")
    if found and _is_executable_file(found):
        return os.path.abspath(os.path.expanduser(found))

    fallbacks: Sequence[str | Path] = (
        static_candidates
        if static_candidates is not None
        else ("/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh",)
    )
    for fb in fallbacks:
        exp_fb = os.path.abspath(os.path.expanduser(str(fb)))
        if _is_executable_file(exp_fb):
            return exp_fb
    return ""


def discover_codex_cli(
    config: AgentConfig,
    *,
    nvm_versions_dir: Path | None = None,
    which_fn: Callable[[str], str | None] = shutil.which,
    static_candidates: Sequence[str | Path] | None = None,
    allow_repo_dotenv_fallback: bool = True,
) -> str:
    """Locate `codex` binary dynamically across `.env`, PATH, `~/.local/bin`, `~/.nvm`, and macOS bundles."""
    live_vars = _read_live_dotenv(config, allow_repo_dotenv_fallback=allow_repo_dotenv_fallback)
    live_p = (live_vars.get("CODEX_CLI_PATH") or "").strip()
    mem_p = (getattr(config, "codex_cli_path", "") or "").strip()
    for raw_c in ((live_p, mem_cli := mem_p) if config.loaded_env_file is not None else (mem_p, live_p)):
        if raw_c:
            exp = os.path.abspath(os.path.expanduser(raw_c))
            if _is_executable_file(exp):
                return exp

    found = which_fn("codex")
    if found and _is_executable_file(found):
        return os.path.abspath(os.path.expanduser(found))

    local_bin = os.path.abspath(os.path.expanduser("~/.local/bin/codex"))
    if static_candidates is None and _is_executable_file(local_bin):
        return local_bin

    nvm_root = (
        Path(nvm_versions_dir).expanduser()
        if nvm_versions_dir is not None
        else Path("~/.nvm/versions/node").expanduser()
    )
    try:
        if nvm_root.is_dir():
            nvm_bins = sorted(
                nvm_root.glob("*/bin/codex"),
                key=lambda p: _parse_node_version_tuple(p.parent.parent.name),
                reverse=True,
            )
            for cand_path in nvm_bins:
                lexical = os.path.abspath(os.path.expanduser(str(cand_path)))
                if _is_executable_file(lexical):
                    return lexical
    except OSError:
        pass

    fallbacks: Sequence[str | Path] = (
        static_candidates
        if static_candidates is not None
        else (
            "~/.local/bin/codex",
            "/Applications/ChatGPT.app/Contents/Resources/codex",
            "~/.codex/plugins/.plugin-appserver/codex",
            "/opt/homebrew/bin/codex",
            "/usr/local/bin/codex",
        )
    )
    for fb in fallbacks:
        exp_fb = os.path.abspath(os.path.expanduser(str(fb)))
        if _is_executable_file(exp_fb):
            return exp_fb
    return ""


def discover_agy_cli(
    config: AgentConfig,
    *,
    which_fn: Callable[[str], str | None] = shutil.which,
    static_candidates: Sequence[str | Path] | None = None,
    allow_repo_dotenv_fallback: bool = True,
) -> str:
    """Locate `agy` binary dynamically across `.env`, PATH, `~/.local/bin`, and Homebrew."""
    live_vars = _read_live_dotenv(config, allow_repo_dotenv_fallback=allow_repo_dotenv_fallback)
    live_p = (live_vars.get("AGY_CLI_PATH") or "").strip()
    mem_p = (getattr(config, "agy_cli_path", "") or "").strip()
    for raw_c in ((live_p, mem_p) if config.loaded_env_file is not None else (mem_p, live_p)):
        if raw_c:
            exp = os.path.abspath(os.path.expanduser(raw_c))
            if _is_executable_file(exp):
                return exp

    found = which_fn("agy")
    if found and _is_executable_file(found):
        return os.path.abspath(os.path.expanduser(found))

    fallbacks: Sequence[str | Path] = (
        static_candidates
        if static_candidates is not None
        else (
            "~/.local/bin/agy",
            "/opt/homebrew/bin/agy",
            "/usr/local/bin/agy",
        )
    )
    for fb in fallbacks:
        exp_fb = os.path.abspath(os.path.expanduser(str(fb)))
        if _is_executable_file(exp_fb):
            return exp_fb
    return ""


# ============================================================================
# Structured Output Parsers & Result Dataclass
# ============================================================================


def _is_stale_session_error(*texts: str) -> bool:
    combined = "\n".join(t for t in texts if t).lower()
    return any(
        phrase in combined
        for phrase in (
            "session not found",
            "unknown session",
            "invalid session",
            "no such session",
            "conversation not found",
            "unknown conversation",
            "thread not found",
            "rollout not found",
            "no rollout found",
            "failed to resume",
            "could not find session",
            "could not find conversation",
        )
    )


def _is_unsupported_model_error(*texts: str) -> bool:
    combined = "\n".join(t for t in texts if t).lower()
    return any(
        phrase in combined
        for phrase in (
            "model is not supported",
            "unsupported model",
            "does not exist",
            "model_not_found",
            "unknown model",
        )
    )


def _redact_home_in_snippet(text: str) -> str:
    out = (text or "").strip()
    home_posix = Path.home().as_posix()
    if home_posix:
        out = out.replace(home_posix, "~")
    return out


def parse_dsh_output(stdout_str: str) -> tuple[bool, str, str]:
    """Parse `dsh --json` JSONL stream, returning `(is_error, reply_text, session_id)`."""
    raw = (stdout_str or "").strip()
    if not raw:
        return (False, "", "")

    discovered_sid = ""
    final_texts: list[str] = []
    fallback_texts: list[str] = []
    any_error = False
    parsed_any_json = False

    for line in raw.splitlines():
        s_line = line.strip()
        if not s_line.startswith("{"):
            continue
        try:
            obj = json.loads(s_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        parsed_any_json = True
        sid_cand = obj.get("sessionId") or obj.get("session_id")
        if isinstance(sid_cand, str) and sid_cand.strip():
            discovered_sid = sid_cand.strip()

        ev_type = str(obj.get("type") or "").lower()
        if ev_type == "error" or obj.get("is_error") is True:
            any_error = True
            err_msg = obj.get("message") or obj.get("error") or obj.get("text")
            if isinstance(err_msg, str) and err_msg.strip():
                fallback_texts.append(err_msg.strip())
            continue

        if ev_type in ("final", "result"):
            txt = obj.get("text") or obj.get("result") or obj.get("reply") or obj.get("content")
            if isinstance(txt, str) and txt.strip():
                final_texts.append(txt.strip())
            else:
                _, sub_txt = _extract_from_parsed_json(obj)
                if sub_txt:
                    final_texts.append(sub_txt)
        elif ev_type in ("assistant", "message", "text"):
            _, sub_txt = _extract_from_parsed_json(obj)
            if sub_txt:
                fallback_texts.append(sub_txt)

    if final_texts:
        return (any_error, final_texts[-1], discovered_sid)
    if fallback_texts:
        return (any_error, "\n".join(fallback_texts).strip(), discovered_sid)
    if parsed_any_json:
        return (any_error, "", discovered_sid)
    return (False, raw, "")


def parse_codex_jsonl(stdout_str: str) -> tuple[bool, str, str]:
    """Parse `codex exec --json` JSONL stream, returning `(is_error, fallback_reply, thread_id)`."""
    raw = (stdout_str or "").strip()
    if not raw:
        return (False, "", "")

    thread_id = ""
    agent_messages: list[str] = []
    errors: list[str] = []
    parsed_any = False

    for line in raw.splitlines():
        s_line = line.strip()
        if not s_line.startswith("{"):
            continue
        try:
            obj = json.loads(s_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        parsed_any = True
        ev_type = str(obj.get("type") or "").lower()
        tid_cand = obj.get("thread_id") or obj.get("threadId")
        if isinstance(tid_cand, str) and tid_cand.strip():
            thread_id = tid_cand.strip()

        if ev_type in ("error", "turn.failed"):
            err_val = obj.get("message") or obj.get("error")
            if isinstance(err_val, dict):
                err_val = err_val.get("message") or json.dumps(err_val, ensure_ascii=False)
            if isinstance(err_val, str) and err_val.strip():
                errors.append(err_val.strip())
            continue

        item = obj.get("item")
        if isinstance(item, dict):
            i_type = str(item.get("type") or "").lower()
            if i_type in ("agent_message", "message", "assistant_message", "output_text"):
                txt = item.get("text")
                if isinstance(txt, str) and txt.strip():
                    agent_messages.append(txt.strip())
                elif isinstance(item.get("content"), list):
                    _, sub_txt = _extract_from_parsed_json({"content": item["content"]})
                    if sub_txt:
                        agent_messages.append(sub_txt)
        elif ev_type in ("agent_message", "result", "final"):
            _, sub_txt = _extract_from_parsed_json(obj)
            if sub_txt:
                agent_messages.append(sub_txt)

    if agent_messages:
        return (False, agent_messages[-1], thread_id)
    if errors:
        return (True, errors[-1], thread_id)
    if parsed_any:
        return (False, "", thread_id)
    return (False, raw, "")


def parse_agy_output(stdout_str: str) -> tuple[bool, str, str]:
    """Parse `agy --output-format json -p ...` output (JSON dict, JSONL, or plain text)."""
    raw = (stdout_str or "").strip()
    if not raw:
        return (False, "", "")

    def _extract_agy_obj(obj: Any) -> tuple[bool, str, str]:
        conv_id = ""
        if isinstance(obj, dict):
            for cid_key in ("conversation_id", "conversationId", "session_id", "sessionId"):
                val = obj.get(cid_key)
                if isinstance(val, str) and val.strip():
                    conv_id = val.strip()
                    break
            is_err = bool(
                obj.get("is_error") is True
                or str(obj.get("type") or "").lower() == "error"
                or (obj.get("error") and not obj.get("response") and not obj.get("result"))
            )
            for key in ("response", "result", "reply", "text", "output", "content", "message", "error"):
                if key not in obj or obj[key] is None:
                    continue
                v = obj[key]
                if isinstance(v, str) and v.strip():
                    return (is_err or key == "error", v.strip(), conv_id)
                sub_err, sub_txt = _extract_from_parsed_json(v)
                if sub_txt:
                    return (is_err or sub_err, sub_txt, conv_id)
            return (is_err, "", conv_id)
        if isinstance(obj, list):
            last_txt = ""
            any_err = False
            for it in obj:
                e_i, t_i, c_i = _extract_agy_obj(it)
                any_err = any_err or e_i
                if c_i:
                    conv_id = c_i
                if t_i:
                    last_txt = t_i
            return (any_err, last_txt, conv_id)
        return (False, "", "")

    try:
        loaded = json.loads(raw)
        is_err, txt, cid = _extract_agy_obj(loaded)
        if txt or cid:
            return (is_err, txt, cid)
    except json.JSONDecodeError:
        pass

    jsonl_items: list[Any] = []
    for line in raw.splitlines():
        s_line = line.strip()
        if not (s_line.startswith("{") or s_line.startswith("[")):
            continue
        try:
            jsonl_items.append(json.loads(s_line))
        except json.JSONDecodeError:
            continue
    if jsonl_items:
        is_err, txt, cid = _extract_agy_obj(jsonl_items)
        if txt or cid:
            return (is_err, txt, cid)

    return (False, raw, "")


@dataclasses.dataclass
class HarnessResult:
    """Structured execution result from `DSHClient`, `CodexClient`, or `AGYClient`."""

    ok: bool
    harness: str
    exit_code: int
    reply: str
    formatted_reply: str
    deliverable_paths: list[Path]
    session_id: str
    model_used: str = ""
    error: str = ""


# ============================================================================
# 1. DSHClient (DeepSeek Harness)
# ============================================================================


class DSHClient:
    """Async headless client for DeepSeek Harness (`dsh`) with Magpie gateway patch & JSONL session resume."""

    SESSION_META_PREFIX = "dsh_session_id:"
    APP_RUNTIME_BIN = "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/bin"

    def __init__(
        self,
        config: AgentConfig,
        *,
        runner: SubprocessRunnerCallable | None = None,
        cli_path_override: str | None = None,
        dsh_home_dir: str | Path | None = None,
        magpie_probe: Callable[[str, int], bool] | None = None,
        allow_repo_dotenv_fallback: bool = True,
        timeout_override: float | None = None,
    ) -> None:
        self.config = config
        self._runner = runner
        self._cli_path_override = cli_path_override
        self._dsh_home_dir = (
            Path(dsh_home_dir).expanduser()
            if dsh_home_dir is not None
            else Path("~/.dsh").expanduser()
        )
        self._magpie_probe = magpie_probe or _probe_tcp_port
        self._allow_repo_dotenv_fallback = allow_repo_dotenv_fallback
        self._timeout_override = timeout_override
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def resolve_cli_and_model(self) -> tuple[str, str]:
        live_vars = _read_live_dotenv(
            self.config, allow_repo_dotenv_fallback=self._allow_repo_dotenv_fallback
        )
        if self._cli_path_override is not None:
            cli_path = self._cli_path_override.strip()
        else:
            cli_path = discover_dsh_cli(
                self.config, allow_repo_dotenv_fallback=self._allow_repo_dotenv_fallback
            )
        live_model = (live_vars.get("DSH_MODEL") or "").strip()
        mem_model = (getattr(self.config, "dsh_model", "") or "").strip()
        model = (
            (live_model or mem_model)
            if self.config.loaded_env_file is not None
            else (mem_model or live_model)
        )
        return (cli_path, model)

    def check_readiness(self) -> dict[str, Any]:
        cli_path, _ = self.resolve_cli_and_model()
        t_val = max(
            float(getattr(self.config, "dsh_timeout_sec", MIN_HARNESS_TIMEOUT_SEC)),
            MIN_HARNESS_TIMEOUT_SEC,
        )
        return {
            "cli_found": bool(cli_path),
            "timeout_sec": int(t_val) if t_val.is_integer() else t_val,
        }

    @classmethod
    def _meta_key(cls, sender: str) -> str:
        return f"{cls.SESSION_META_PREFIX}{normalize_sender(sender) or 'owner'}"

    def get_session_id(self, store: "StateStore | None", sender: str = "") -> str:
        if store is None:
            return ""
        return (store.get_meta(self._meta_key(sender)) or "").strip()

    def reset_session(self, store: "StateStore", sender: str = "") -> None:
        store.set_meta(self._meta_key(sender), "")

    def _is_magpie_available(self) -> bool:
        settings_path = self._dsh_home_dir / "settings.yaml"
        try:
            if not settings_path.is_file():
                return False
            txt = settings_path.read_text(encoding="utf-8", errors="ignore")
            if "magpie:" not in txt:
                return False
        except OSError:
            return False
        return bool(self._magpie_probe(DEFAULT_MAGPIE_HOST, DEFAULT_MAGPIE_PORT))

    @staticmethod
    def _write_magpie_patch(workspace_dir: Path, model_name: str) -> Path:
        patch_path = workspace_dir / ".ht_magpie_model.patch.yml"
        eff_model = (model_name or "").strip() or DEFAULT_DSH_MAGPIE_MODEL
        content = (
            "- id: agent-default-model\n"
            "  config:\n"
            "    provider: magpie\n"
            f"    model: {json.dumps(eff_model, ensure_ascii=False)}\n"
            '    reasoningEffort: "high"\n'
        )
        patch_path.write_text(content, encoding="utf-8")
        return patch_path

    async def _invoke_once(
        self, cmd: list[str], cwd: Path, env: dict[str, str], timeout_sec: float
    ) -> tuple[int, str, str]:
        if self._runner is not None:
            return await asyncio.wait_for(
                self._runner(cmd, cwd, env, timeout_sec), timeout=timeout_sec
            )
        return await _default_subprocess_runner(cmd, cwd, env, timeout_sec)

    async def ask(
        self,
        prompt: str,
        *,
        sender: str = "",
        store: "StateStore | None" = None,
        session_id: str | None = None,
        _timeout_override: float | None = None,
    ) -> HarnessResult:
        clean_prompt = (prompt or "").strip()
        saved_sid = (
            session_id.strip()
            if session_id is not None
            else self.get_session_id(store, sender)
        )
        if not clean_prompt:
            err_txt = "请直接附上要交给 DSH 处理的任务，例如：/dsh 帮我分析这段架构方案"
            return HarnessResult(
                ok=False,
                harness="dsh",
                exit_code=400,
                reply="",
                formatted_reply=err_txt,
                deliverable_paths=[],
                session_id=saved_sid,
                error=err_txt,
            )

        cli_path, configured_model = self.resolve_cli_and_model()
        if not cli_path:
            err_txt = (
                "未在本机检测到 DeepSeek Harness 命令行（dsh），请确认已安装 DeepSeek Harness.app "
                "或在 .env 中配置 DSH_CLI_PATH，无需重启即可直接调用。"
            )
            return HarnessResult(
                ok=False,
                harness="dsh",
                exit_code=127,
                reply="",
                formatted_reply=err_txt,
                deliverable_paths=[],
                session_id=saved_sid,
                error=err_txt,
            )

        workspace_dir = self.config.state_dir / "dsh_workspace"
        workspace_dir.mkdir(parents=True, exist_ok=True)

        env = dict(os.environ)
        path_dirs: list[str] = [os.path.dirname(os.path.abspath(os.path.expanduser(cli_path)))]
        if os.path.isdir(self.APP_RUNTIME_BIN):
            path_dirs.insert(0, self.APP_RUNTIME_BIN)
        _prepend_lexical_dirs_to_path(env, path_dirs)
        env["DSH_PERMISSION_MODE"] = "danger-full-access"
        ensure_localhost_no_proxy(env)

        patch_args: list[str] = []
        used_model = configured_model or DEFAULT_DSH_MAGPIE_MODEL
        if self._is_magpie_available():
            patch_file = self._write_magpie_patch(workspace_dir, used_model)
            patch_args = ["--patch", str(patch_file)]
        else:
            if getattr(self.config, "deepseek_api_key", ""):
                env["DEEPSEEK_API_KEY"] = self.config.deepseek_api_key

        safe_prompt = f" {clean_prompt}" if clean_prompt.startswith("-") else clean_prompt
        override = _timeout_override if _timeout_override is not None else self._timeout_override
        eff_timeout = (
            max(0.02, float(override))
            if override is not None
            else max(
                float(getattr(self.config, "dsh_timeout_sec", MIN_HARNESS_TIMEOUT_SEC)),
                MIN_HARNESS_TIMEOUT_SEC,
            )
        )

        lock_key = normalize_sender(sender) or "owner"
        async with self._get_lock(lock_key):
            before_snap = snapshot_workspace_files(workspace_dir)

            def _build_cmd(sid: str) -> list[str]:
                return [
                    cli_path,
                    "--profile",
                    "headless",
                    *patch_args,
                    "--approval-policy",
                    "never",
                    *(["--session-id", sid] if sid else []),
                    "--json",
                    safe_prompt,
                ]

            try:
                rc, stdout_str, stderr_str = await self._invoke_once(
                    _build_cmd(saved_sid), workspace_dir, env, eff_timeout
                )
            except asyncio.CancelledError:
                raise
            except (asyncio.TimeoutError, TimeoutError):
                err_txt = "等待本机 DSH 处理超时了，任务可能较重，稍后可拆分子任务再试。"
                return HarnessResult(
                    ok=False,
                    harness="dsh",
                    exit_code=124,
                    reply="",
                    formatted_reply=err_txt,
                    deliverable_paths=[],
                    session_id=saved_sid,
                    model_used=used_model,
                    error=err_txt,
                )
            except Exception:
                logger.warning("DSH CLI invocation failed", exc_info=True)
                err_txt = "调用本机 DSH 命令行时出了点状况，请检查 DeepSeek Harness 状态后再试。"
                return HarnessResult(
                    ok=False,
                    harness="dsh",
                    exit_code=1,
                    reply="",
                    formatted_reply=err_txt,
                    deliverable_paths=[],
                    session_id=saved_sid,
                    model_used=used_model,
                    error=err_txt,
                )

            is_err, reply_txt, new_sid = parse_dsh_output(stdout_str)
            if saved_sid and (
                _is_stale_session_error(stdout_str, stderr_str, reply_txt)
                or ((rc != 0 or is_err) and not reply_txt)
            ):
                if store is not None:
                    store.set_meta(self._meta_key(sender), "")
                saved_sid = ""
                try:
                    rc, stdout_str, stderr_str = await self._invoke_once(
                        _build_cmd(""), workspace_dir, env, eff_timeout
                    )
                    is_err, reply_txt, new_sid = parse_dsh_output(stdout_str)
                except asyncio.CancelledError:
                    raise
                except (asyncio.TimeoutError, TimeoutError):
                    err_txt = "等待本机 DSH 处理超时了，任务可能较重，稍后可拆分子任务再试。"
                    return HarnessResult(
                        ok=False,
                        harness="dsh",
                        exit_code=124,
                        reply="",
                        formatted_reply=err_txt,
                        deliverable_paths=[],
                        session_id="",
                        model_used=used_model,
                        error=err_txt,
                    )

            final_sid = new_sid or saved_sid
            if new_sid and store is not None:
                store.set_meta(self._meta_key(sender), new_sid)

            deliverables = collect_changed_deliverables(workspace_dir, before_snap)
            if rc != 0 and not reply_txt:
                snippet = _redact_home_in_snippet(
                    ((stderr_str or "").strip().splitlines() or [f"exit={rc}"])[-1][:140]
                )
                err_txt = f"DSH 执行未成功（{snippet}），请稍后重试。"
                return HarnessResult(
                    ok=False,
                    harness="dsh",
                    exit_code=rc,
                    reply="",
                    formatted_reply=err_txt,
                    deliverable_paths=deliverables,
                    session_id=final_sid,
                    model_used=used_model,
                    error=err_txt,
                )

            clean_reply = reply_txt.strip()
            formatted = format_for_imessage(clean_reply) or (
                "DSH 已生成结果文件，请查收附件。" if deliverables else "DSH 已处理完成。"
            )
            return HarnessResult(
                ok=not is_err and rc == 0,
                harness="dsh",
                exit_code=rc,
                reply=clean_reply,
                formatted_reply=formatted,
                deliverable_paths=deliverables,
                session_id=final_sid,
                model_used=used_model,
                error=clean_reply if (is_err or rc != 0) else "",
            )


# ============================================================================
# 2. CodexClient (OpenAI Codex CLI)
# ============================================================================


class CodexClient:
    """Async headless client for OpenAI Codex CLI (`codex exec` / `codex exec resume`) with model chain."""

    THREAD_META_PREFIX = "codex_thread_id:"

    def __init__(
        self,
        config: AgentConfig,
        *,
        runner: SubprocessRunnerCallable | None = None,
        cli_path_override: str | None = None,
        proxy_probe: Callable[[str, int], bool] | None = None,
        allow_repo_dotenv_fallback: bool = True,
        timeout_override: float | None = None,
    ) -> None:
        self.config = config
        self._runner = runner
        self._cli_path_override = cli_path_override
        self._proxy_probe = proxy_probe
        self._allow_repo_dotenv_fallback = allow_repo_dotenv_fallback
        self._timeout_override = timeout_override
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def resolve_cli_and_model_chain(self) -> tuple[str, list[str]]:
        live_vars = _read_live_dotenv(
            self.config, allow_repo_dotenv_fallback=self._allow_repo_dotenv_fallback
        )
        if self._cli_path_override is not None:
            cli_path = self._cli_path_override.strip()
        else:
            cli_path = discover_codex_cli(
                self.config, allow_repo_dotenv_fallback=self._allow_repo_dotenv_fallback
            )

        live_m = (live_vars.get("CODEX_MODEL") or "").strip()
        mem_m = (getattr(self.config, "codex_model", "") or "").strip()
        explicit_m = (
            (live_m or mem_m)
            if self.config.loaded_env_file is not None
            else (mem_m or live_m)
        )
        chain: list[str] = []
        for candidate in ([explicit_m] if explicit_m else []) + list(DEFAULT_CODEX_MODEL_CHAIN):
            if candidate and candidate not in chain:
                chain.append(candidate)
        return (cli_path, chain)

    def check_readiness(self) -> dict[str, Any]:
        cli_path, _ = self.resolve_cli_and_model_chain()
        t_val = max(
            float(getattr(self.config, "codex_timeout_sec", MIN_HARNESS_TIMEOUT_SEC)),
            MIN_HARNESS_TIMEOUT_SEC,
        )
        return {
            "cli_found": bool(cli_path),
            "timeout_sec": int(t_val) if t_val.is_integer() else t_val,
        }

    @classmethod
    def _meta_key(cls, sender: str) -> str:
        return f"{cls.THREAD_META_PREFIX}{normalize_sender(sender) or 'owner'}"

    def get_thread_id(self, store: "StateStore | None", sender: str = "") -> str:
        if store is None:
            return ""
        return (store.get_meta(self._meta_key(sender)) or "").strip()

    def reset_session(self, store: "StateStore", sender: str = "") -> None:
        store.set_meta(self._meta_key(sender), "")

    async def _invoke_once(
        self, cmd: list[str], cwd: Path, env: dict[str, str], timeout_sec: float
    ) -> tuple[int, str, str]:
        if self._runner is not None:
            return await asyncio.wait_for(
                self._runner(cmd, cwd, env, timeout_sec), timeout=timeout_sec
            )
        return await _default_subprocess_runner(cmd, cwd, env, timeout_sec)

    async def ask(
        self,
        prompt: str,
        *,
        sender: str = "",
        store: "StateStore | None" = None,
        thread_id: str | None = None,
        _timeout_override: float | None = None,
    ) -> HarnessResult:
        clean_prompt = (prompt or "").strip()
        saved_tid = (
            thread_id.strip()
            if thread_id is not None
            else self.get_thread_id(store, sender)
        )
        if not clean_prompt:
            err_txt = "请直接附上要交给 Codex 处理的任务，例如：/codex 帮我审查这段并发逻辑"
            return HarnessResult(
                ok=False,
                harness="codex",
                exit_code=400,
                reply="",
                formatted_reply=err_txt,
                deliverable_paths=[],
                session_id=saved_tid,
                error=err_txt,
            )

        cli_path, model_chain = self.resolve_cli_and_model_chain()
        if not cli_path:
            err_txt = (
                "未在本机检测到 OpenAI Codex 命令行（codex），请先安装 Codex CLI "
                "或在 .env 中配置 CODEX_CLI_PATH，无需重启即可直接调用。"
            )
            return HarnessResult(
                ok=False,
                harness="codex",
                exit_code=127,
                reply="",
                formatted_reply=err_txt,
                deliverable_paths=[],
                session_id=saved_tid,
                error=err_txt,
            )

        workspace_dir = self.config.state_dir / "codex_workspace"
        workspace_dir.mkdir(parents=True, exist_ok=True)

        env = dict(os.environ)
        _prepend_lexical_dirs_to_path(
            env, [os.path.dirname(os.path.abspath(os.path.expanduser(cli_path)))]
        )
        apply_local_proxy_env(
            env,
            self.config,
            proxy_probe=self._proxy_probe,
            allow_repo_dotenv_fallback=self._allow_repo_dotenv_fallback,
        )

        safe_prompt = f" {clean_prompt}" if clean_prompt.startswith("-") else clean_prompt
        override = _timeout_override if _timeout_override is not None else self._timeout_override
        eff_timeout = (
            max(0.02, float(override))
            if override is not None
            else max(
                float(getattr(self.config, "codex_timeout_sec", MIN_HARNESS_TIMEOUT_SEC)),
                MIN_HARNESS_TIMEOUT_SEC,
            )
        )

        lock_key = normalize_sender(sender) or "owner"
        async with self._get_lock(lock_key):
            before_snap = snapshot_workspace_files(workspace_dir)
            out_file = workspace_dir / f".ht_codex_out_{uuid.uuid4().hex[:8]}.txt"

            def _build_codex_cmd(model: str, tid: str) -> list[str]:
                if tid:
                    return [
                        cli_path,
                        "exec",
                        "resume",
                        tid,
                        "-m",
                        model,
                        "-c",
                        "notify=[]",
                        "-c",
                        "mcp_servers={}",
                        "-c",
                        'model_reasoning_effort="medium"',
                        "--dangerously-bypass-approvals-and-sandbox",
                        "--skip-git-repo-check",
                        "--json",
                        "-o",
                        str(out_file),
                        safe_prompt,
                    ]
                return [
                    cli_path,
                    "exec",
                    "-m",
                    model,
                    "-c",
                    "notify=[]",
                    "-c",
                    "mcp_servers={}",
                    "-c",
                    'model_reasoning_effort="medium"',
                    "--dangerously-bypass-approvals-and-sandbox",
                    "--skip-git-repo-check",
                    "--json",
                    "-C",
                    str(workspace_dir),
                    "-o",
                    str(out_file),
                    safe_prompt,
                ]

            rc = 1
            stdout_str = ""
            stderr_str = ""
            used_model = model_chain[0]
            active_tid = saved_tid
            file_reply = ""

            try:
                for idx, candidate_model in enumerate(model_chain):
                    used_model = candidate_model
                    out_file.unlink(missing_ok=True)
                    try:
                        rc, stdout_str, stderr_str = await self._invoke_once(
                            _build_codex_cmd(candidate_model, active_tid),
                            workspace_dir,
                            env,
                            eff_timeout,
                        )
                    except asyncio.CancelledError:
                        raise
                    except (asyncio.TimeoutError, TimeoutError):
                        err_txt = "等待本机 Codex 处理超时了，任务可能较重，稍后可拆分子任务再试。"
                        return HarnessResult(
                            ok=False,
                            harness="codex",
                            exit_code=124,
                            reply="",
                            formatted_reply=err_txt,
                            deliverable_paths=[],
                            session_id=active_tid,
                            model_used=used_model,
                            error=err_txt,
                        )
                    except Exception:
                        logger.warning("Codex CLI invocation failed", exc_info=True)
                        err_txt = "调用本机 Codex 命令行时出了点状况，请检查 Codex 登录与安装状态后再试。"
                        return HarnessResult(
                            ok=False,
                            harness="codex",
                            exit_code=1,
                            reply="",
                            formatted_reply=err_txt,
                            deliverable_paths=[],
                            session_id=active_tid,
                            model_used=used_model,
                            error=err_txt,
                        )

                    if _is_unsupported_model_error(stdout_str, stderr_str) and (
                        idx + 1 < len(model_chain)
                    ):
                        logger.info(
                            "Codex model %s unsupported; falling back to %s",
                            candidate_model,
                            model_chain[idx + 1],
                        )
                        continue

                    # If `exec resume <tid>` failed on stale/invalid thread_id, clear and retry fresh `exec`
                    if active_tid and rc != 0 and not _is_unsupported_model_error(
                        stdout_str, stderr_str
                    ):
                        if store is not None:
                            store.set_meta(self._meta_key(sender), "")
                        active_tid = ""
                        out_file.unlink(missing_ok=True)
                        try:
                            rc, stdout_str, stderr_str = await self._invoke_once(
                                _build_codex_cmd(candidate_model, ""),
                                workspace_dir,
                                env,
                                eff_timeout,
                            )
                        except asyncio.CancelledError:
                            raise
                        except (asyncio.TimeoutError, TimeoutError):
                            err_txt = "等待本机 Codex 处理超时了，任务可能较重，稍后可拆分子任务再试。"
                            return HarnessResult(
                                ok=False,
                                harness="codex",
                                exit_code=124,
                                reply="",
                                formatted_reply=err_txt,
                                deliverable_paths=[],
                                session_id="",
                                model_used=used_model,
                                error=err_txt,
                            )
                        if _is_unsupported_model_error(stdout_str, stderr_str) and (
                            idx + 1 < len(model_chain)
                        ):
                            continue

                    break

                if out_file.is_file():
                    with contextlib.suppress(OSError):
                        file_reply = out_file.read_text(encoding="utf-8", errors="replace").strip()
            finally:
                out_file.unlink(missing_ok=True)

            is_err, jsonl_reply, discovered_tid = parse_codex_jsonl(stdout_str)
            final_tid = discovered_tid or active_tid
            if discovered_tid and store is not None:
                store.set_meta(self._meta_key(sender), discovered_tid)

            deliverables = collect_changed_deliverables(workspace_dir, before_snap)
            reply_txt = file_reply or jsonl_reply

            if rc != 0 and not reply_txt:
                snippet = _redact_home_in_snippet(
                    ((stderr_str or stdout_str or "").strip().splitlines() or [f"exit={rc}"])[-1][
                        :140
                    ]
                )
                err_txt = f"Codex 执行未成功（{snippet}），请稍后重试。"
                return HarnessResult(
                    ok=False,
                    harness="codex",
                    exit_code=rc,
                    reply="",
                    formatted_reply=err_txt,
                    deliverable_paths=deliverables,
                    session_id=final_tid,
                    model_used=used_model,
                    error=err_txt,
                )

            clean_reply = reply_txt.strip()
            formatted = format_for_imessage(clean_reply) or (
                "Codex 已生成结果文件，请查收附件。" if deliverables else "Codex 已处理完成。"
            )
            return HarnessResult(
                ok=not is_err and rc == 0,
                harness="codex",
                exit_code=rc,
                reply=clean_reply,
                formatted_reply=formatted,
                deliverable_paths=deliverables,
                session_id=final_tid,
                model_used=used_model,
                error=clean_reply if (is_err or rc != 0) else "",
            )


# ============================================================================
# 3. AGYClient (Google Antigravity CLI)
# ============================================================================


class AGYClient:
    """Async headless client for Google Antigravity CLI (`agy`) with strict trailing `['-p', prompt]`."""

    CONV_META_PREFIX = "agy_conv_id:"

    def __init__(
        self,
        config: AgentConfig,
        *,
        runner: SubprocessRunnerCallable | None = None,
        cli_path_override: str | None = None,
        proxy_probe: Callable[[str, int], bool] | None = None,
        allow_repo_dotenv_fallback: bool = True,
        timeout_override: float | None = None,
    ) -> None:
        self.config = config
        self._runner = runner
        self._cli_path_override = cli_path_override
        self._proxy_probe = proxy_probe
        self._allow_repo_dotenv_fallback = allow_repo_dotenv_fallback
        self._timeout_override = timeout_override
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def resolve_cli_and_model(self) -> tuple[str, str]:
        live_vars = _read_live_dotenv(
            self.config, allow_repo_dotenv_fallback=self._allow_repo_dotenv_fallback
        )
        if self._cli_path_override is not None:
            cli_path = self._cli_path_override.strip()
        else:
            cli_path = discover_agy_cli(
                self.config, allow_repo_dotenv_fallback=self._allow_repo_dotenv_fallback
            )
        live_m = (live_vars.get("AGY_MODEL") or "").strip()
        mem_m = (getattr(self.config, "agy_model", "") or "").strip()
        model = (
            (live_m or mem_m)
            if self.config.loaded_env_file is not None
            else (mem_m or live_m)
        ) or DEFAULT_AGY_MODEL
        return (cli_path, model)

    def check_readiness(self) -> dict[str, Any]:
        cli_path, model = self.resolve_cli_and_model()
        t_val = max(
            float(getattr(self.config, "agy_timeout_sec", MIN_HARNESS_TIMEOUT_SEC)),
            MIN_HARNESS_TIMEOUT_SEC,
        )
        return {
            "cli_found": bool(cli_path),
            "model": model,
            "timeout_sec": int(t_val) if t_val.is_integer() else t_val,
        }

    @classmethod
    def _meta_key(cls, sender: str) -> str:
        return f"{cls.CONV_META_PREFIX}{normalize_sender(sender) or 'owner'}"

    def get_conversation_id(self, store: "StateStore | None", sender: str = "") -> str:
        if store is None:
            return ""
        return (store.get_meta(self._meta_key(sender)) or "").strip()

    def reset_session(self, store: "StateStore", sender: str = "") -> None:
        store.set_meta(self._meta_key(sender), "")

    async def _invoke_once(
        self, cmd: list[str], cwd: Path, env: dict[str, str], timeout_sec: float
    ) -> tuple[int, str, str]:
        if self._runner is not None:
            return await asyncio.wait_for(
                self._runner(cmd, cwd, env, timeout_sec), timeout=timeout_sec
            )
        return await _default_subprocess_runner(cmd, cwd, env, timeout_sec)

    async def ask(
        self,
        prompt: str,
        *,
        sender: str = "",
        store: "StateStore | None" = None,
        conversation_id: str | None = None,
        _timeout_override: float | None = None,
    ) -> HarnessResult:
        clean_prompt = (prompt or "").strip()
        saved_cid = (
            conversation_id.strip()
            if conversation_id is not None
            else self.get_conversation_id(store, sender)
        )
        if not clean_prompt:
            err_txt = "请直接附上要交给 AGY 处理的任务，例如：/agy 帮我设计一套前端状态机方案"
            return HarnessResult(
                ok=False,
                harness="agy",
                exit_code=400,
                reply="",
                formatted_reply=err_txt,
                deliverable_paths=[],
                session_id=saved_cid,
                error=err_txt,
            )

        cli_path, used_model = self.resolve_cli_and_model()
        if not cli_path:
            err_txt = (
                "未在本机检测到 Antigravity 命令行（agy），请先安装 agy CLI "
                "或在 .env 中配置 AGY_CLI_PATH，无需重启即可直接调用。"
            )
            return HarnessResult(
                ok=False,
                harness="agy",
                exit_code=127,
                reply="",
                formatted_reply=err_txt,
                deliverable_paths=[],
                session_id=saved_cid,
                error=err_txt,
            )

        workspace_dir = self.config.state_dir / "agy_workspace"
        workspace_dir.mkdir(parents=True, exist_ok=True)

        env = dict(os.environ)
        _prepend_lexical_dirs_to_path(
            env, [os.path.dirname(os.path.abspath(os.path.expanduser(cli_path)))]
        )
        apply_local_proxy_env(
            env,
            self.config,
            proxy_probe=self._proxy_probe,
            allow_repo_dotenv_fallback=self._allow_repo_dotenv_fallback,
        )

        override = _timeout_override if _timeout_override is not None else self._timeout_override
        eff_timeout = (
            max(0.02, float(override))
            if override is not None
            else max(
                float(getattr(self.config, "agy_timeout_sec", MIN_HARNESS_TIMEOUT_SEC)),
                MIN_HARNESS_TIMEOUT_SEC,
            )
        )

        lock_key = normalize_sender(sender) or "owner"
        async with self._get_lock(lock_key):
            before_snap = snapshot_workspace_files(workspace_dir)

            def _build_agy_cmd(cid: str, model_name: str) -> list[str]:
                # CRITICAL FLAG ORDER: `"-p", clean_prompt` MUST be at the VERY END of `argv`!
                return [
                    cli_path,
                    "--model",
                    model_name,
                    "--dangerously-skip-permissions",
                    "--disable-slash-commands",
                    "--print-timeout",
                    "8m",
                    *(["--conversation", cid] if cid else []),
                    "--output-format",
                    "json",
                    "-p",
                    clean_prompt,
                ]

            def _is_agy_model_fallback_error(out_s: str, err_s: str, parsed_txt: str) -> bool:
                hay = f"{out_s}\n{err_s}\n{parsed_txt}".lower()
                return any(
                    tok in hay
                    for tok in (
                        "quota",
                        "resource_exhausted",
                        "exhausted",
                        "capacity",
                        "overloaded",
                        "model not found",
                        "unknown model",
                        "not supported",
                        "unavailable",
                        "429",
                    )
                )

            try:
                rc, stdout_str, stderr_str = await self._invoke_once(
                    _build_agy_cmd(saved_cid, used_model), workspace_dir, env, eff_timeout
                )
            except asyncio.CancelledError:
                raise
            except (asyncio.TimeoutError, TimeoutError):
                err_txt = "等待本机 AGY 处理超时了，任务可能较重，稍后可拆分子任务再试。"
                return HarnessResult(
                    ok=False,
                    harness="agy",
                    exit_code=124,
                    reply="",
                    formatted_reply=err_txt,
                    deliverable_paths=[],
                    session_id=saved_cid,
                    model_used=used_model,
                    error=err_txt,
                )
            except Exception:
                logger.warning("AGY CLI invocation failed", exc_info=True)
                err_txt = "调用本机 AGY 命令行时出了点状况，请检查 Antigravity 运行状态后再试。"
                return HarnessResult(
                    ok=False,
                    harness="agy",
                    exit_code=1,
                    reply="",
                    formatted_reply=err_txt,
                    deliverable_paths=[],
                    session_id=saved_cid,
                    model_used=used_model,
                    error=err_txt,
                )

            is_err, reply_txt, new_cid = parse_agy_output(stdout_str)
            if saved_cid and (
                _is_stale_session_error(stdout_str, stderr_str, reply_txt)
                or ((rc != 0 or is_err) and not reply_txt and not _is_agy_model_fallback_error(stdout_str, stderr_str, reply_txt))
            ):
                if store is not None:
                    store.set_meta(self._meta_key(sender), "")
                saved_cid = ""
                try:
                    rc, stdout_str, stderr_str = await self._invoke_once(
                        _build_agy_cmd("", used_model), workspace_dir, env, eff_timeout
                    )
                    is_err, reply_txt, new_cid = parse_agy_output(stdout_str)
                except asyncio.CancelledError:
                    raise
                except (asyncio.TimeoutError, TimeoutError):
                    err_txt = "等待本机 AGY 处理超时了，任务可能较重，稍后可拆分子任务再试。"
                    return HarnessResult(
                        ok=False,
                        harness="agy",
                        exit_code=124,
                        reply="",
                        formatted_reply=err_txt,
                        deliverable_paths=[],
                        session_id="",
                        model_used=used_model,
                        error=err_txt,
                    )

            if (rc != 0 or is_err) and not reply_txt and _is_agy_model_fallback_error(
                stdout_str, stderr_str, reply_txt
            ):
                for fb_model in DEFAULT_AGY_MODEL_CHAIN:
                    if fb_model == used_model:
                        continue
                    try:
                        rc, stdout_str, stderr_str = await self._invoke_once(
                            _build_agy_cmd(saved_cid, fb_model),
                            workspace_dir,
                            env,
                            eff_timeout,
                        )
                        is_err, reply_txt, new_cid = parse_agy_output(stdout_str)
                        used_model = fb_model
                        if rc == 0 and not is_err and reply_txt:
                            break
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        break

            final_cid = new_cid or saved_cid
            if new_cid and store is not None:
                store.set_meta(self._meta_key(sender), new_cid)

            deliverables = collect_changed_deliverables(workspace_dir, before_snap)
            if rc != 0 and not reply_txt:
                snippet = _redact_home_in_snippet(
                    ((stderr_str or "").strip().splitlines() or [f"exit={rc}"])[-1][:140]
                )
                err_txt = f"AGY 执行未成功（{snippet}），请稍后重试。"
                return HarnessResult(
                    ok=False,
                    harness="agy",
                    exit_code=rc,
                    reply="",
                    formatted_reply=err_txt,
                    deliverable_paths=deliverables,
                    session_id=final_cid,
                    model_used=used_model,
                    error=err_txt,
                )

            clean_reply = reply_txt.strip()
            formatted = format_for_imessage(clean_reply) or (
                "AGY 已生成结果文件，请查收附件。" if deliverables else "AGY 已处理完成。"
            )
            return HarnessResult(
                ok=not is_err and rc == 0,
                harness="agy",
                exit_code=rc,
                reply=clean_reply,
                formatted_reply=formatted,
                deliverable_paths=deliverables,
                session_id=final_cid,
                model_used=used_model,
                error=clean_reply if (is_err or rc != 0) else "",
            )
