"""Explicit-Only Admin WorkBuddy (`@tencent-ai/codebuddy-code`) Headless CLI Integration."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import re
import shutil
import signal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping, Sequence

from .config import (
    AgentConfig,
    _parse_dotenv_text,
    _resolve_dotenv_candidates,
    normalize_sender,
)
from .formatter import format_for_imessage, is_sensitive_attachment_path
from .xiaona_client import is_explicit_xiaona_request

if TYPE_CHECKING:
    from .store_sqlite import StateStore

logger = logging.getLogger("hypertext.workbuddy_client")

MIN_WORKBUDDY_TIMEOUT_SEC = 600.0
MAX_WORKBUDDY_DELIVERABLE_BYTES = 25 * 1024 * 1024

_DELIVERABLE_SUFFIXES = {
    ".html",
    ".pptx",
    ".pdf",
    ".docx",
    ".xlsx",
    ".csv",
    ".md",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".mp4",
}

_IGNORED_SCAN_DIRS = {
    "node_modules",
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    "dist",
    "build",
}

# Word-boundary safe patterns so Chinese adjacent text (`让workbuddy查一下`) matches cleanly
# while rejecting `myworkbuddy`, `workbuddy2`, `/wbbot`, `/wb123`.
_WB_FULL_WORD_PAT = r"(?<![A-Za-z0-9_])(?i:workbuddy|codebuddy)(?![A-Za-z0-9_])"
_WB_SLASH_OR_AT_PAT = r"(?i:workbuddy|codebuddy|wb)(?![A-Za-z0-9_])"

_WB_SLASH_OR_AT_RE = re.compile(
    rf"^[/／@]\s*{_WB_SLASH_OR_AT_PAT}(?:$|\s+|[:：,，!！?？.。、~～\-]|(?=[\u4e00-\u9fff]))"
)
_WB_NATURAL_WORD_RE = re.compile(_WB_FULL_WORD_PAT)

_WB_RESET_CMD_RE = re.compile(
    rf"^(?:"
    rf"[/／@]\s*{_WB_SLASH_OR_AT_PAT}\s*(?:[:：]\s*)?(?:reset|clear|重置|清空|重置会话|清空会话|重置上下文|清空上下文)"
    rf"|{_WB_FULL_WORD_PAT}\s*(?:[:：]\s*)?(?:reset|clear)"
    rf"|(?:重置|清空)\s*{_WB_FULL_WORD_PAT}\s*(?:的)?(?:会话|上下文|对话)?"
    rf")$",
    re.IGNORECASE,
)

_STRIP_WB_SLASH_AT_RE = re.compile(rf"^[/／@]\s*{_WB_SLASH_OR_AT_PAT}[\s:：,，、\-]*")
_STRIP_WB_DELEGATION_RE = re.compile(
    rf"^(?:帮我|麻烦你|麻烦|请你)?\s*"
    rf"(?:问下|问一下|问问|请问|让|请|叫|找|喊|麻烦|呼叫|唤醒|转给|交给|发给|委托|调用|用)\s*"
    rf"{_WB_FULL_WORD_PAT}"
    rf"(?:\s*(?:回答一下|回答下|回答|说一下|说下|处理一下|处理下|跑一下|跑下))?"
    rf"[\s:：,，、]*"
)
_STRIP_WB_WITH_RE = re.compile(
    rf"^(?:帮我|麻烦你|麻烦|请你)?\s*跟\s*{_WB_FULL_WORD_PAT}\s*(?:说一下|说下|说|问一下|问下|问)?[\s:：,，、]*"
)
_STRIP_WB_VOCATIVE_RE = re.compile(
    rf"^(?:嘿|嗨|喂|你好|哈喽)?[\s,，!！]*{_WB_FULL_WORD_PAT}(?:\s*[:：,，!！、~～]+|\s+|$)"
)

_WB_COMPOUND_LOCAL_ACTION_RE = re.compile(
    r"(?:"
    r"定个闹钟|设个闹钟|定闹钟|设闹钟"
    r"|设个提醒|定个提醒|设提醒|定提醒|加个提醒|创建提醒|定时提醒"
    r"|分钟后提醒|小时后提醒|点提醒我|点半提醒我|提醒我一下"
    r"|用本地浏览器|打开本地浏览器|打开本地网页|在本机打开|截屏|截图"
    r")"
)

_AUTH_REQUIRED_HINT = (
    "WorkBuddy 命令行已接入就绪，还差一步登录授权：请在 Mac mini 终端运行一次 codebuddy，"
    "输入 /login 完成扫码/账号登录，之后无需重启服务即可直接在手机上调用。"
)
_CLI_MISSING_HINT = (
    "未在本机检测到 WorkBuddy 命令行（codebuddy），请先安装 @tencent-ai/codebuddy-code "
    "或在 .env 中设置 WORKBUDDY_CLI_PATH，无需重启即可直接调用。"
)


def _clean_inbound_text(text: str) -> str:
    return (text or "").replace("\u2063", "").replace("\ufffc", "").replace("\x00", "").strip()


def is_workbuddy_reset_command(text: str) -> bool:
    """Return True if text explicitly resets WorkBuddy session context."""
    cleaned = _clean_inbound_text(text)
    cleaned = re.sub(r"[!！?？.。~～\s]+$", "", cleaned).strip()
    if not cleaned:
        return False
    return bool(_WB_RESET_CMD_RE.fullmatch(cleaned))


def is_explicit_workbuddy_request(text: str) -> bool:
    """Return True ONLY when user text explicitly invokes `workbuddy`, `codebuddy`, or `/wb`."""
    cleaned = _clean_inbound_text(text)
    if not cleaned:
        return False
    if is_workbuddy_reset_command(cleaned):
        return True
    if _WB_SLASH_OR_AT_RE.search(cleaned):
        return True
    if _WB_NATURAL_WORD_RE.search(cleaned):
        return True
    return False


def extract_workbuddy_query(text: str) -> str:
    """Strip leading `/wb`, `/workbuddy`, `@workbuddy`, `让 workbuddy 回答：` wrappers cleanly."""
    cleaned = _clean_inbound_text(text)
    if not cleaned:
        return ""
    for pattern in (
        _STRIP_WB_SLASH_AT_RE,
        _STRIP_WB_DELEGATION_RE,
        _STRIP_WB_WITH_RE,
        _STRIP_WB_VOCATIVE_RE,
    ):
        updated, count = pattern.subn("", cleaned, count=1)
        if count > 0:
            return updated.strip()
    return cleaned


def is_compound_workbuddy_request(text: str) -> bool:
    """Return True when an explicit WorkBuddy request ALSO invokes XiaoNa/XiaoMiGuan or local Mac tools."""
    cleaned = _clean_inbound_text(text)
    if not is_explicit_workbuddy_request(cleaned) or is_workbuddy_reset_command(cleaned):
        return False
    if is_explicit_xiaona_request(cleaned):
        return True
    return bool(_WB_COMPOUND_LOCAL_ACTION_RE.search(cleaned))


def _parse_node_version_tuple(folder_name: str) -> tuple[int, ...]:
    nums = [int(tok) for tok in re.findall(r"\d+", folder_name or "")]
    return tuple(nums) if nums else (0,)


def _is_executable_file(lexical_path: str) -> bool:
    if not lexical_path:
        return False
    expanded = os.path.expanduser(lexical_path.strip())
    return os.path.isfile(expanded) and os.access(expanded, os.X_OK)


def _read_live_dotenv_vars(
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


def discover_workbuddy_cli(
    config: AgentConfig,
    *,
    nvm_versions_dir: Path | None = None,
    which_fn: Callable[[str], str | None] = shutil.which,
    static_candidates: Sequence[str | Path] | None = None,
    allow_repo_dotenv_fallback: bool = True,
) -> str:
    """Dynamically locate `codebuddy` CLI binary without resolving symlinks (preserving `.nvm/.../bin`)."""
    # 1. Explicit config or live .env `WORKBUDDY_CLI_PATH`
    live_vars = _read_live_dotenv_vars(
        config, allow_repo_dotenv_fallback=allow_repo_dotenv_fallback
    )
    live_cli = (live_vars.get("WORKBUDDY_CLI_PATH") or "").strip()
    mem_cli = (getattr(config, "workbuddy_cli_path", "") or "").strip()
    for raw_cand in (
        (live_cli, mem_cli) if config.loaded_env_file is not None else (mem_cli, live_cli)
    ):
        if raw_cand:
            expanded = os.path.abspath(os.path.expanduser(raw_cand))
            if _is_executable_file(expanded):
                return expanded

    # 2. System PATH lookup (`codebuddy` or `cbc`)
    for bin_name in ("codebuddy", "cbc"):
        found = which_fn(bin_name)
        if found and _is_executable_file(found):
            return os.path.abspath(os.path.expanduser(found))

    # 3. NVM installations sorted by descending semantic version (preserving lexical bin/codebuddy path!)
    nvm_root = (
        Path(nvm_versions_dir).expanduser()
        if nvm_versions_dir is not None
        else Path("~/.nvm/versions/node").expanduser()
    )
    try:
        if nvm_root.is_dir():
            nvm_bins = sorted(
                nvm_root.glob("*/bin/codebuddy"),
                key=lambda p: _parse_node_version_tuple(p.parent.parent.name),
                reverse=True,
            )
            for cand_path in nvm_bins:
                lexical = os.path.abspath(os.path.expanduser(str(cand_path)))
                if _is_executable_file(lexical):
                    return lexical
    except OSError:
        pass

    # 4. Standard macOS Homebrew / /usr/local / WorkBuddy.app bundle locations
    fallbacks: Sequence[str | Path] = (
        static_candidates
        if static_candidates is not None
        else (
            "/opt/homebrew/bin/codebuddy",
            "/usr/local/bin/codebuddy",
            "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy",
        )
    )
    for fb in fallbacks:
        lexical_fb = os.path.abspath(os.path.expanduser(str(fb)))
        if _is_executable_file(lexical_fb):
            return lexical_fb

    return ""


def build_workbuddy_child_env(
    cli_path: str,
    *,
    base_env: Mapping[str, str] | None = None,
    config_dir: str = "",
) -> dict[str, str]:
    """Build subprocess environment prepending lexical `dirname(cli_path)` so `#!/usr/bin/env node` succeeds."""
    env = dict(base_env if base_env is not None else os.environ)
    if cli_path:
        # CRITICAL: Do NOT call os.path.realpath() / Path.resolve()!
        # `codebuddy` in ~/.nvm/versions/node/vX.Y.Z/bin/codebuddy symlinks to ../lib/node_modules/...,
        # whereas `node` lives in ~/.nvm/versions/node/vX.Y.Z/bin!
        cli_bin_dir = os.path.dirname(os.path.abspath(os.path.expanduser(cli_path)))
        current_path = env.get("PATH", "")
        path_parts = current_path.split(os.pathsep) if current_path else []
        if not path_parts or path_parts[0] != cli_bin_dir:
            env["PATH"] = (
                f"{cli_bin_dir}{os.pathsep}{current_path}" if current_path else cli_bin_dir
            )

    if config_dir and config_dir.strip():
        expanded_cfg = os.path.abspath(os.path.expanduser(config_dir.strip()))
        env["CODEBUDDY_CONFIG_DIR"] = expanded_cfg
        env["WORKBUDDY_CONFIG_DIR"] = expanded_cfg
    return env


def _extract_text_from_blocks(items: list[Any]) -> str:
    parts: list[str] = []
    for it in items:
        if isinstance(it, str) and it.strip():
            parts.append(it.strip())
        elif isinstance(it, dict):
            b_type = str(it.get("type") or "").lower()
            if b_type in ("", "text", "output_text", "result"):
                txt = it.get("text") or it.get("result") or it.get("content")
                if isinstance(txt, str) and txt.strip():
                    parts.append(txt.strip())
    return "\n".join(parts).strip()


def _extract_from_parsed_json(obj: Any) -> tuple[bool, str]:
    if isinstance(obj, str):
        return (False, obj.strip())

    if isinstance(obj, dict):
        is_err = bool(
            obj.get("is_error") is True
            or str(obj.get("type") or "").lower() == "error"
            or str(obj.get("subtype") or "").lower() == "error"
        )
        for key in ("result", "reply", "output", "content", "message", "text", "error"):
            if key not in obj or obj[key] is None:
                continue
            val = obj[key]
            if isinstance(val, str) and val.strip():
                return (is_err or key == "error", val.strip())
            if isinstance(val, list):
                block_txt = _extract_text_from_blocks(val)
                if block_txt:
                    return (is_err, block_txt)
            if isinstance(val, dict):
                sub_err, sub_txt = _extract_from_parsed_json(val)
                if sub_txt:
                    return (is_err or sub_err, sub_txt)
        return (is_err, "")

    if isinstance(obj, list):
        # Prefer terminal `type == "result"` event if present in event stream
        for item in reversed(obj):
            if isinstance(item, dict) and str(item.get("type") or "").lower() == "result":
                sub_err, sub_txt = _extract_from_parsed_json(item)
                if sub_txt:
                    return (sub_err, sub_txt)

        collected: list[str] = []
        any_err = False
        for item in obj:
            sub_err, sub_txt = _extract_from_parsed_json(item)
            any_err = any_err or sub_err
            if sub_txt:
                collected.append(sub_txt)
        return (any_err, "\n".join(collected).strip())

    return (False, "")


def parse_workbuddy_output(stdout_str: str) -> tuple[bool, str]:
    """Parse `codebuddy -p --output-format json` stdout (JSON dict, event list, JSONL, or plain text)."""
    raw = (stdout_str or "").strip()
    if not raw:
        return (False, "")

    try:
        loaded = json.loads(raw)
        is_err, extracted = _extract_from_parsed_json(loaded)
        if extracted:
            return (is_err, extracted)
    except json.JSONDecodeError:
        pass

    # Multi-line JSONL fallback
    jsonl_objs: list[Any] = []
    for line in raw.splitlines():
        s_line = line.strip()
        if not (s_line.startswith("{") or s_line.startswith("[")):
            continue
        try:
            jsonl_objs.append(json.loads(s_line))
        except json.JSONDecodeError:
            continue
    if jsonl_objs:
        is_err, extracted = _extract_from_parsed_json(jsonl_objs)
        if extracted:
            return (is_err, extracted)

    return (False, raw)


def _is_auth_failure_text(*texts: str) -> bool:
    combined = "\n".join(t for t in texts if t).lower()
    return (
        "authentication required" in combined
        or "authentication failed" in combined
        or "please run /login" in combined
        or "not logged in" in combined
    )


def snapshot_workspace_files(workspace_dir: Path) -> dict[str, tuple[int, int]]:
    """Snapshot `(rel_posix -> (st_mtime_ns, st_size))` for non-hidden files inside `workspace_dir`."""
    snap: dict[str, tuple[int, int]] = {}
    if not workspace_dir.is_dir():
        return snap
    try:
        ws_real = workspace_dir.resolve()
    except OSError:
        return snap

    for root, dirs, files in os.walk(workspace_dir, followlinks=False):
        dirs[:] = [
            d for d in dirs if not d.startswith(".") and d not in _IGNORED_SCAN_DIRS
        ]
        root_path = Path(root)
        for fname in files:
            if fname.startswith("."):
                continue
            fpath = root_path / fname
            try:
                if fpath.is_symlink():
                    continue
                resolved = fpath.resolve()
                resolved.relative_to(ws_real)
                st = fpath.stat()
                if not os.path.isfile(fpath):
                    continue
                rel_key = fpath.relative_to(workspace_dir).as_posix()
                snap[rel_key] = (int(st.st_mtime_ns), int(st.st_size))
            except (OSError, ValueError):
                continue
    return snap


def collect_changed_deliverables(
    workspace_dir: Path,
    before_snap: Mapping[str, tuple[int, int]],
    max_bytes: int = MAX_WORKBUDDY_DELIVERABLE_BYTES,
) -> list[Path]:
    """Return newly created or modified safe deliverable files (`<= 25 MB`) sorted by mtime_ns."""
    if not workspace_dir.is_dir():
        return []
    try:
        ws_real = workspace_dir.resolve()
    except OSError:
        return []

    matched: list[tuple[int, str, Path]] = []
    for root, dirs, files in os.walk(workspace_dir, followlinks=False):
        dirs[:] = [
            d for d in dirs if not d.startswith(".") and d not in _IGNORED_SCAN_DIRS
        ]
        root_path = Path(root)
        for fname in files:
            if fname.startswith("."):
                continue
            fpath = root_path / fname
            if fpath.suffix.lower() not in _DELIVERABLE_SUFFIXES:
                continue
            try:
                if fpath.is_symlink():
                    continue
                resolved = fpath.resolve()
                resolved.relative_to(ws_real)
                st = fpath.stat()
                if st.st_size <= 0 or st.st_size > max_bytes:
                    continue
                rel_key = fpath.relative_to(workspace_dir).as_posix()
                sig = (int(st.st_mtime_ns), int(st.st_size))
                if before_snap.get(rel_key) == sig:
                    continue
                blocked, _ = is_sensitive_attachment_path(fpath)
                if blocked:
                    continue
                matched.append((int(st.st_mtime_ns), rel_key, fpath))
            except (OSError, ValueError):
                continue

    matched.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in matched]


@dataclasses.dataclass
class WorkBuddyResult:
    """Structured execution result from `WorkBuddyClient.ask`."""

    ok: bool
    exit_code: int
    reply: str
    formatted_reply: str
    deliverable_paths: list[Path]
    session_id: str
    error: str = ""


SubprocessRunnerCallable = Callable[
    [list[str], Path, dict[str, str], float], Awaitable[tuple[int, str, str]]
]


async def _default_subprocess_runner(
    cmd: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout_sec: float,
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_sec
        )
    except (asyncio.TimeoutError, TimeoutError, asyncio.CancelledError):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.communicate(), timeout=2.0)
        raise

    out_str = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
    err_str = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
    return (int(proc.returncode or 0), out_str, err_str)


class WorkBuddyClient:
    """Async headless runner for `@tencent-ai/codebuddy-code` with dual config-dir fallback & deliverable scan."""

    SESSION_SEQ_META_PREFIX = "workbuddy_session_seq:"

    def __init__(
        self,
        config: AgentConfig,
        *,
        runner: SubprocessRunnerCallable | None = None,
        cli_path_override: str | None = None,
        alt_config_dir: str | Path | None = None,
        allow_repo_dotenv_fallback: bool = True,
        timeout_override: float | None = None,
    ) -> None:
        self.config = config
        self._runner = runner
        self._cli_path_override = cli_path_override
        self._alt_config_dir = (
            Path(alt_config_dir).expanduser()
            if alt_config_dir is not None
            else Path("~/.workbuddy").expanduser()
        )
        self._allow_repo_dotenv_fallback = allow_repo_dotenv_fallback
        self._timeout_override = timeout_override
        self._session_locks: dict[str, asyncio.Lock] = {}

    def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    def resolve_cli_and_settings(self) -> tuple[str, str, str]:
        """Hot-resolve `(cli_path, explicit_config_dir, model_name)` without daemon restart."""
        live_vars = _read_live_dotenv_vars(
            self.config, allow_repo_dotenv_fallback=self._allow_repo_dotenv_fallback
        )

        if self._cli_path_override is not None:
            cli_path = self._cli_path_override.strip()
        else:
            cli_path = discover_workbuddy_cli(
                self.config,
                allow_repo_dotenv_fallback=self._allow_repo_dotenv_fallback,
            )

        live_cfg_dir = (live_vars.get("WORKBUDDY_CONFIG_DIR") or "").strip()
        mem_cfg_dir = (getattr(self.config, "workbuddy_config_dir", "") or "").strip()
        explicit_cfg_dir = (
            (live_cfg_dir or mem_cfg_dir)
            if self.config.loaded_env_file is not None
            else (mem_cfg_dir or live_cfg_dir)
        )

        live_model = (live_vars.get("WORKBUDDY_MODEL") or "").strip()
        mem_model = (getattr(self.config, "workbuddy_model", "") or "").strip()
        model_name = (
            (live_model or mem_model)
            if self.config.loaded_env_file is not None
            else (mem_model or live_model)
        )
        return (cli_path, explicit_cfg_dir, model_name)

    def check_readiness(self) -> dict[str, Any]:
        """Return masked boolean readiness dict for `--doctor` (never exposing personal paths)."""
        cli_path, _, _ = self.resolve_cli_and_settings()
        timeout_val = max(
            float(getattr(self.config, "workbuddy_timeout_sec", MIN_WORKBUDDY_TIMEOUT_SEC)),
            MIN_WORKBUDDY_TIMEOUT_SEC,
        )
        return {
            "cli_found": bool(cli_path),
            "timeout_sec": int(timeout_val) if timeout_val.is_integer() else timeout_val,
        }

    @classmethod
    def _session_meta_key(cls, sender: str) -> str:
        norm = normalize_sender(sender) or "owner"
        return f"{cls.SESSION_SEQ_META_PREFIX}{norm}"

    def get_session_id(self, store: "StateStore | None", sender: str = "") -> str:
        """Return persistent WorkBuddy session ID (`ht-wb-owner-<seq>`, default `ht-wb-owner-1`)."""
        seq = 1
        if store is not None:
            raw = store.get_meta(self._session_meta_key(sender))
            if raw:
                with contextlib.suppress(ValueError):
                    seq = max(1, int(raw.strip()))
        return f"ht-wb-owner-{seq}"

    def reset_session(self, store: "StateStore", sender: str = "") -> str:
        """Increment persistent WorkBuddy session counter (`ht-wb-owner-1` -> `ht-wb-owner-2`)."""
        meta_key = self._session_meta_key(sender)
        seq = 1
        raw = store.get_meta(meta_key)
        if raw:
            with contextlib.suppress(ValueError):
                seq = max(1, int(raw.strip()))
        next_seq = seq + 1
        store.set_meta(meta_key, str(next_seq))
        return f"ht-wb-owner-{next_seq}"

    async def _invoke_once(
        self,
        cmd: list[str],
        cwd: Path,
        env: dict[str, str],
        timeout_sec: float,
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
    ) -> WorkBuddyResult:
        """Run an explicit owner task in headless WorkBuddy (`codebuddy -p`) under per-session lock."""
        eff_session_id = (
            session_id.strip()
            if session_id and session_id.strip()
            else self.get_session_id(store, sender)
        )
        clean_prompt = (prompt or "").strip()
        if not clean_prompt:
            err_txt = "请直接附上要交给 WorkBuddy 处理的任务，例如：/wb 帮我生成一份竞品对比 HTML 卡片"
            return WorkBuddyResult(
                ok=False,
                exit_code=400,
                reply="",
                formatted_reply=err_txt,
                deliverable_paths=[],
                session_id=eff_session_id,
                error=err_txt,
            )

        cli_path, explicit_cfg_dir, model_name = self.resolve_cli_and_settings()
        if not cli_path:
            return WorkBuddyResult(
                ok=False,
                exit_code=127,
                reply="",
                formatted_reply=_CLI_MISSING_HINT,
                deliverable_paths=[],
                session_id=eff_session_id,
                error=_CLI_MISSING_HINT,
            )

        workspace_dir = self.config.state_dir / "workbuddy_workspace"
        workspace_dir.mkdir(parents=True, exist_ok=True)

        safe_prompt = f" {clean_prompt}" if clean_prompt.startswith("-") else clean_prompt
        cmd: list[str] = [
            cli_path,
            "-p",
            "--output-format",
            "json",
            "-y",
            "--session-id",
            eff_session_id,
            "-r",
            eff_session_id,
            "--resume-create-missing",
        ]
        if model_name:
            cmd.extend(["--model", model_name])
        cmd.append(safe_prompt)

        override = _timeout_override if _timeout_override is not None else self._timeout_override
        if override is not None:
            eff_timeout = max(0.02, float(override))
        else:
            eff_timeout = max(
                float(getattr(self.config, "workbuddy_timeout_sec", MIN_WORKBUDDY_TIMEOUT_SEC)),
                MIN_WORKBUDDY_TIMEOUT_SEC,
            )

        lock = self._get_session_lock(eff_session_id)
        async with lock:
            before_snap = snapshot_workspace_files(workspace_dir)
            primary_env = build_workbuddy_child_env(
                cli_path, config_dir=explicit_cfg_dir
            )
            try:
                rc, stdout_str, stderr_str = await self._invoke_once(
                    cmd, workspace_dir, primary_env, eff_timeout
                )
            except asyncio.CancelledError:
                raise
            except (asyncio.TimeoutError, TimeoutError):
                logger.warning(
                    "WorkBuddy CLI timed out after %.1fs (session_id=%s)",
                    eff_timeout,
                    eff_session_id,
                )
                err_txt = "等待 WorkBuddy 本机处理超时了，任务可能较重，稍后可换个拆分子任务再试。"
                return WorkBuddyResult(
                    ok=False,
                    exit_code=124,
                    reply="",
                    formatted_reply=err_txt,
                    deliverable_paths=[],
                    session_id=eff_session_id,
                    error=err_txt,
                )
            except Exception:
                logger.warning(
                    "WorkBuddy CLI invocation failed (session_id=%s)",
                    eff_session_id,
                    exc_info=True,
                )
                err_txt = "调用本机 WorkBuddy 命令行时出了点状况，请检查 codebuddy 安装状态后再试。"
                return WorkBuddyResult(
                    ok=False,
                    exit_code=1,
                    reply="",
                    formatted_reply=err_txt,
                    deliverable_paths=[],
                    session_id=eff_session_id,
                    error=err_txt,
                )

            is_err, parsed_text = parse_workbuddy_output(stdout_str)
            auth_failed = _is_auth_failure_text(stdout_str, stderr_str, parsed_text)

            # Dual config-dir auto-fallback: `~/.codebuddy` auth error -> retry once with `~/.workbuddy`
            if (
                auth_failed
                and not explicit_cfg_dir
                and self._alt_config_dir.is_dir()
            ):
                alt_env = build_workbuddy_child_env(
                    cli_path, config_dir=str(self._alt_config_dir)
                )
                try:
                    rc, stdout_str, stderr_str = await self._invoke_once(
                        cmd, workspace_dir, alt_env, eff_timeout
                    )
                    is_err, parsed_text = parse_workbuddy_output(stdout_str)
                    auth_failed = _is_auth_failure_text(stdout_str, stderr_str, parsed_text)
                except asyncio.CancelledError:
                    raise
                except (asyncio.TimeoutError, TimeoutError):
                    err_txt = "等待 WorkBuddy 本机处理超时了，任务可能较重，稍后可换个拆分子任务再试。"
                    return WorkBuddyResult(
                        ok=False,
                        exit_code=124,
                        reply="",
                        formatted_reply=err_txt,
                        deliverable_paths=[],
                        session_id=eff_session_id,
                        error=err_txt,
                    )
                except Exception:
                    logger.warning("WorkBuddy alt config-dir retry failed", exc_info=True)

            if auth_failed:
                return WorkBuddyResult(
                    ok=False,
                    exit_code=401,
                    reply="",
                    formatted_reply=_AUTH_REQUIRED_HINT,
                    deliverable_paths=[],
                    session_id=eff_session_id,
                    error=_AUTH_REQUIRED_HINT,
                )

            deliverables = collect_changed_deliverables(workspace_dir, before_snap)

            if rc != 0 and not parsed_text:
                clean_stderr = (stderr_str or "").strip().splitlines()
                brief_err = clean_stderr[-1][:140] if clean_stderr else f"exit={rc}"
                # Redact any potential home path from stderr snippet
                home_posix = Path.home().as_posix()
                if home_posix:
                    brief_err = brief_err.replace(home_posix, "~")
                err_txt = f"WorkBuddy 执行未成功（{brief_err}），请稍后重试。"
                return WorkBuddyResult(
                    ok=False,
                    exit_code=rc,
                    reply="",
                    formatted_reply=err_txt,
                    deliverable_paths=deliverables,
                    session_id=eff_session_id,
                    error=err_txt,
                )

            reply_clean = parsed_text.strip()
            formatted = format_for_imessage(reply_clean)
            if not formatted:
                formatted = (
                    "WorkBuddy 已生成结果文件，请查收附件。"
                    if deliverables
                    else "WorkBuddy 已处理完成。"
                )

            return WorkBuddyResult(
                ok=not is_err and rc == 0,
                exit_code=rc,
                reply=reply_clean,
                formatted_reply=formatted,
                deliverable_paths=deliverables,
                session_id=eff_session_id,
                error=reply_clean if (is_err or rc != 0) else "",
            )
