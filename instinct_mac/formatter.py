"""XiaoNa SMS Readability Formatter, Rich Link Unpacker, Mobile HTML Card Generator & Attachment Guards."""

from __future__ import annotations

import datetime as dt
import fnmatch
import html
import pathlib
import re
from pathlib import Path

# Markdown link: [Title](https://example.com/path(with_parens)) or ![Alt](https://...)
_MD_LINK_RE = re.compile(
    r"!?\[([^\[\]\n]*)\]\(\s*<?(https?://(?:[^\s()<>]+|\([^\s()<>]*\))+)?>?\s*\)"
)
_ANGLE_URL_RE = re.compile(r"<(https?://[^\s<>]+)>")
_FENCED_CODE_RE = re.compile(r"```[a-zA-Z0-9_+#.-]*[ \t]*\n?(.*?)\n?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_HEADING_RE = re.compile(r"(?m)^[ \t]*#{1,6}[ \t]+(.+?)[ \t]*#*$")
_HR_RE = re.compile(r"(?m)^[ \t]*(?:[-]{3,}|[*]{3,}|[_]{3,})[ \t]*$")
_BLOCKQUOTE_RE = re.compile(r"(?m)^[ \t]*>[ \t]?")
_BOLD_STAR_RE = re.compile(r"\*\*(.+?)\*\*")
_BOLD_UNDER_RE = re.compile(r"__(.+?)__")
_STRIKE_RE = re.compile(r"~~(.+?)~~")
# Careful single-asterisk italic: requires non-space inside, NOT flanked by digits (preserves 3 * 4 and 1920*1080)
_ITALIC_STAR_RE = re.compile(r"(?<![0-9*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![0-9*])")
# Careful single-underscore italic: never touches URLs or snake_case identifiers
_ITALIC_UNDER_RE = re.compile(
    r"(?<![A-Za-z0-9:/._-])_(?!\s)([^_\n]+?)(?<!\s)_(?![A-Za-z0-9/_-])"
)
_BULLET_RE = re.compile(r"(?m)^([ \t]*)[-*+][ \t]+")
_BARE_URL_TOKEN_RE = re.compile(r"https?://[^\s<>\"'`]+")

_TRAILING_GLUED_PUNCT = ".,;:!?)]}>，。；：！？）】》」』”’"
_LEADING_GLUED_WRAPPERS = "([<（【《「『“‘"


def _clean_url_trailing_punct(raw_url: str) -> tuple[str, str]:
    """Split trailing prose/Markdown punctuation from a URL while preserving balanced URL parens & query/fragment."""
    url = raw_url
    suffix_chars: list[str] = []
    while url and url[-1] in _TRAILING_GLUED_PUNCT:
        ch = url[-1]
        if ch == ")" and url.count("(") >= url.count(")"):
            break
        if ch == "]" and url.count("[") >= url.count("]"):
            break
        suffix_chars.append(ch)
        url = url[:-1]
    suffix_chars.reverse()
    return url, "".join(suffix_chars)


def _strip_inline_md(s: str) -> str:
    """Strip inline Markdown markers from a single table cell or title."""
    s = _INLINE_CODE_RE.sub(r"\1", s)
    s = _BOLD_STAR_RE.sub(r"\1", s)
    s = _BOLD_UNDER_RE.sub(r"\1", s)
    s = _STRIKE_RE.sub(r"\1", s)
    s = _ITALIC_STAR_RE.sub(r"\1", s)
    s = _ITALIC_UNDER_RE.sub(r"\1", s)
    return s.strip()


def _is_table_separator_line(line: str) -> bool:
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return False
    cells = [c.strip() for c in stripped.strip("|").split("|")]
    if not cells:
        return False
    return all(bool(c) and all(ch in "-: " for ch in c) and "-" in c for c in cells)


def _split_table_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [_strip_inline_md(cell.strip()) for cell in stripped.split("|")]


def _convert_markdown_tables(text: str) -> str:
    """Convert Markdown pipe tables into mobile-friendly bullet rows (`· H1：V1 ｜ H2：V2`)."""
    lines = text.split("\n")
    out: list[str] = []
    idx = 0
    n = len(lines)

    while idx < n:
        line = lines[idx]
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2:
            block: list[str] = []
            while idx < n:
                cand = lines[idx].strip()
                if cand.startswith("|") and cand.endswith("|") and cand.count("|") >= 2:
                    block.append(lines[idx])
                    idx += 1
                else:
                    break

            sep_positions = [i for i, bl in enumerate(block) if _is_table_separator_line(bl)]
            if sep_positions:
                headers = _split_table_row(block[0]) if sep_positions[0] >= 1 else []
                for row_i, bl in enumerate(block):
                    if _is_table_separator_line(bl):
                        continue
                    if row_i == 0 and sep_positions[0] >= 1:
                        continue
                    vals = _split_table_row(bl)
                    pairs: list[str] = []
                    for col_i, val in enumerate(vals):
                        if not val:
                            continue
                        hdr = headers[col_i] if col_i < len(headers) else ""
                        if hdr:
                            pairs.append(f"{hdr}：{val}")
                        else:
                            pairs.append(val)
                    if pairs:
                        out.append("· " + " ｜ ".join(pairs))
                continue

            # Pipe rows without a separator row: still clean pipes gracefully
            for bl in block:
                if _is_table_separator_line(bl):
                    continue
                vals = [v for v in _split_table_row(bl) if v]
                if vals:
                    out.append("· " + " ｜ ".join(vals))
            continue

        out.append(line)
        idx += 1

    return "\n".join(out)


def _isolate_standalone_urls(text: str) -> str:
    """Ensure every http(s) URL stands cleanly on its own line without glued brackets/punctuation."""
    out_lines: list[str] = []
    for line in text.split("\n"):
        if "http://" not in line and "https://" not in line:
            out_lines.append(line)
            continue

        matches = list(_BARE_URL_TOKEN_RE.finditer(line))
        if not matches:
            out_lines.append(line)
            continue

        cursor = 0
        segments: list[str] = []
        for m in matches:
            start, end = m.span()
            before = line[cursor:start]
            raw_url = m.group(0)
            clean_url, trailing = _clean_url_trailing_punct(raw_url)

            # Strip trailing wrapper characters like '(' or '[' or '（' immediately preceding the URL
            before_clean = before.rstrip()
            had_open_wrapper = False
            while before_clean and before_clean[-1] in _LEADING_GLUED_WRAPPERS:
                before_clean = before_clean[:-1].rstrip()
                had_open_wrapper = True

            if before_clean:
                segments.append(before_clean)
            segments.append(clean_url)

            # Drop closing wrapper if we stripped its opening wrapper right before the URL
            if had_open_wrapper and trailing and trailing[0] in ")]>）】》」』”’":
                trailing = trailing[1:]
            cursor = end
            if trailing:
                # Keep meaningful prose after trailing punctuation if any
                trailing_clean = trailing.lstrip(".,;:!?，。；：！？)]>）】")
                if trailing_clean:
                    segments.append(trailing_clean)

        tail = line[cursor:].strip()
        if tail:
            tail_clean = tail.lstrip(")]>）】》")
            if tail_clean:
                segments.append(tail_clean)

        out_lines.extend(seg for seg in segments if seg)

    return "\n".join(out_lines)


def format_for_imessage(text: str) -> str:
    """Convert LLM output into clean, zero-Markdown human SMS text with standalone Rich Link URLs."""
    if not text:
        return ""

    s = text.replace("\r\n", "\n").replace("\r", "\n")

    # 1. Strip fenced code block backticks while keeping inner content
    s = _FENCED_CODE_RE.sub(lambda m: "\n" + m.group(1).strip("\n") + "\n", s)

    # 2. Unpack Markdown links [Title](https://...) BEFORE table/inline processing so URLs are never mangled
    def _unpack_md_link(match: re.Match[str]) -> str:
        raw_label = _strip_inline_md(match.group(1) or "")
        raw_url = (match.group(2) or "").strip()
        clean_url, _ = _clean_url_trailing_punct(raw_url)
        if (
            not raw_label
            or raw_label == clean_url
            or clean_url.startswith(raw_label)
            or raw_label.rstrip("/") == clean_url.rstrip("/")
        ):
            return f"\n{clean_url}\n"
        return f"{raw_label}\n{clean_url}\n"

    s = _MD_LINK_RE.sub(_unpack_md_link, s)
    s = _ANGLE_URL_RE.sub(lambda m: f"\n{m.group(1).strip()}\n", s)

    # 3. Convert Markdown tables into mobile-readable bullet rows
    s = _convert_markdown_tables(s)

    # 4. Protect URLs with placeholders while stripping inline Markdown (* / _ / ~ / `)
    protected_urls: list[str] = []

    def _stash_url(match: re.Match[str]) -> str:
        clean_url, suffix = _clean_url_trailing_punct(match.group(0))
        idx = len(protected_urls)
        protected_urls.append(clean_url)
        return f"@@INSTINCTURL{idx}@@{suffix}"

    s = _BARE_URL_TOKEN_RE.sub(_stash_url, s)

    # 5. Strip Markdown headings, horizontal rules, blockquotes, inline code, bold/italic/strike, bullets
    s = _HR_RE.sub("", s)
    s = _HEADING_RE.sub(r"\n\1\n", s)
    s = _BLOCKQUOTE_RE.sub("", s)
    s = _INLINE_CODE_RE.sub(r"\1", s)
    s = _BOLD_STAR_RE.sub(r"\1", s)
    s = _BOLD_UNDER_RE.sub(r"\1", s)
    s = _STRIKE_RE.sub(r"\1", s)
    s = _ITALIC_STAR_RE.sub(r"\1", s)
    s = _ITALIC_UNDER_RE.sub(r"\1", s)
    s = _BULLET_RE.sub(r"\1· ", s)

    # 6. Restore protected URLs and isolate each URL onto its own clean line for iMessage Rich Link Cards
    for idx, url_val in enumerate(protected_urls):
        s = s.replace(f"@@INSTINCTURL{idx}@@", url_val)

    s = _isolate_standalone_urls(s)

    # 7. Normalize trailing spaces and blank lines
    cleaned_lines = [ln.rstrip() for ln in s.split("\n")]
    s = "\n".join(cleaned_lines)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _format_inline_html(raw: str, discovered_urls: list[str]) -> str:
    """Safely escape inline text (XSS-proof) while turning http(s) links, **bold**, and `code` into clean HTML."""
    tokens: list[str] = []

    def _save_token(html_fragment: str) -> str:
        tok = f"@@HTMLTOK{len(tokens)}@@"
        tokens.append(html_fragment)
        return tok

    # 1. Extract Markdown links [Title](https://...)
    def _md_link_to_tok(m: re.Match[str]) -> str:
        label = (m.group(1) or "").strip()
        url, _ = _clean_url_trailing_punct((m.group(2) or "").strip())
        if not (url.startswith("http://") or url.startswith("https://")):
            return html.escape(m.group(0), quote=True)
        if url not in discovered_urls:
            discovered_urls.append(url)
        safe_href = html.escape(url, quote=True)
        safe_label = html.escape(label or url, quote=True)
        return _save_token(
            f'<a href="{safe_href}" target="_blank" rel="noopener noreferrer">{safe_label}</a>'
        )

    work = _MD_LINK_RE.sub(_md_link_to_tok, raw)

    # 2. Extract bare http(s) URLs
    def _bare_url_to_tok(m: re.Match[str]) -> str:
        url, suffix = _clean_url_trailing_punct(m.group(0))
        if url not in discovered_urls:
            discovered_urls.append(url)
        safe_href = html.escape(url, quote=True)
        tok = _save_token(
            f'<a href="{safe_href}" target="_blank" rel="noopener noreferrer">{safe_href}</a>'
        )
        return tok + suffix

    work = _BARE_URL_TOKEN_RE.sub(_bare_url_to_tok, work)

    # 3. Escape all remaining raw text against XSS
    escaped = html.escape(work, quote=True)

    # 4. Safe inline code and bold on already-escaped text
    escaped = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)

    # 5. Restore safe anchor tokens
    for idx, frag in enumerate(tokens):
        escaped = escaped.replace(f"@@HTMLTOK{idx}@@", frag)

    return escaped


def render_mobile_html_card(
    title: str,
    body: str,
    source_links: list[str] | None = None,
) -> str:
    """Render structured report/content into a standalone, zero-dependency, XSS-safe mobile HTML5 card."""
    safe_title = html.escape((title or "小纳整理卡片").strip(), quote=True)
    timestamp_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    discovered_urls: list[str] = []

    norm_body = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = norm_body.split("\n")
    blocks_html: list[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # Fenced code block
        if stripped.startswith("```"):
            code_lines: list[str] = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            if i < n:
                i += 1
            blocks_html.append(
                f"<pre><code>{html.escape(chr(10).join(code_lines), quote=True)}</code></pre>"
            )
            continue

        # Markdown pipe table
        if stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2:
            t_lines: list[str] = []
            while i < n:
                cand = lines[i].strip()
                if cand.startswith("|") and cand.endswith("|") and cand.count("|") >= 2:
                    t_lines.append(lines[i])
                    i += 1
                else:
                    break
            data_rows = [r for r in t_lines if not _is_table_separator_line(r)]
            if data_rows:
                hdr_cells = [c.strip() for c in data_rows[0].strip().strip("|").split("|")]
                thead = "".join(
                    f"<th>{_format_inline_html(c, discovered_urls)}</th>" for c in hdr_cells
                )
                tbody_rows: list[str] = []
                for dr in data_rows[1:]:
                    cells = [c.strip() for c in dr.strip().strip("|").split("|")]
                    tds = "".join(
                        f"<td>{_format_inline_html(c, discovered_urls)}</td>" for c in cells
                    )
                    tbody_rows.append(f"<tr>{tds}</tr>")
                blocks_html.append(
                    f'<div class="table-wrap"><table><thead><tr>{thead}</tr></thead>'
                    f"<tbody>{''.join(tbody_rows)}</tbody></table></div>"
                )
            continue

        # Horizontal rule
        if _HR_RE.match(stripped):
            blocks_html.append("<hr>")
            i += 1
            continue

        # Heading
        hm = re.match(r"^(#{1,6})\s+(.+?)\s*#*$", stripped)
        if hm:
            lvl = min(3, max(2, len(hm.group(1)) + 1))
            blocks_html.append(
                f"<h{lvl}>{_format_inline_html(hm.group(2), discovered_urls)}</h{lvl}>"
            )
            i += 1
            continue

        # Unordered list
        if re.match(r"^[-*+·]\s+", stripped):
            items: list[str] = []
            while i < n and re.match(r"^[-*+·]\s+", lines[i].strip()):
                item_txt = re.sub(r"^[-*+·]\s+", "", lines[i].strip(), count=1)
                items.append(f"<li>{_format_inline_html(item_txt, discovered_urls)}</li>")
                i += 1
            blocks_html.append(f"<ul>{''.join(items)}</ul>")
            continue

        # Ordered list
        if re.match(r"^\d+[.)、]\s+", stripped):
            items = []
            while i < n and re.match(r"^\d+[.)、]\s+", lines[i].strip()):
                item_txt = re.sub(r"^\d+[.)、]\s+", "", lines[i].strip(), count=1)
                items.append(f"<li>{_format_inline_html(item_txt, discovered_urls)}</li>")
                i += 1
            blocks_html.append(f"<ol>{''.join(items)}</ol>")
            continue

        # Blockquote
        if stripped.startswith(">"):
            q_lines: list[str] = []
            while i < n and lines[i].strip().startswith(">"):
                q_txt = re.sub(r"^>\s?", "", lines[i].strip(), count=1)
                q_lines.append(_format_inline_html(q_txt, discovered_urls))
                i += 1
            blocks_html.append(f"<blockquote>{'<br>'.join(q_lines)}</blockquote>")
            continue

        # Paragraph
        p_lines: list[str] = []
        while i < n:
            cur = lines[i].strip()
            if (
                not cur
                or cur.startswith("```")
                or (cur.startswith("|") and cur.endswith("|") and cur.count("|") >= 2)
                or re.match(r"^#{1,6}\s+", cur)
                or re.match(r"^[-*+·]\s+", cur)
                or re.match(r"^\d+[.)、]\s+", cur)
                or cur.startswith(">")
                or _HR_RE.match(cur)
            ):
                break
            p_lines.append(_format_inline_html(cur, discovered_urls))
            i += 1
        if p_lines:
            blocks_html.append(f"<p>{'<br>'.join(p_lines)}</p>")

    all_links: list[str] = []
    for link in (source_links or []) + discovered_urls:
        clean_l, _ = _clean_url_trailing_punct((link or "").strip())
        if (clean_l.startswith("http://") or clean_l.startswith("https://")) and clean_l not in all_links:
            all_links.append(clean_l)

    pills_section = ""
    if all_links:
        pills: list[str] = []
        for idx_l, url in enumerate(all_links, start=1):
            domain_match = re.match(r"^https?://([^/]+)", url)
            domain = domain_match.group(1) if domain_match else f"来源 {idx_l}"
            safe_u = html.escape(url, quote=True)
            safe_d = html.escape(domain, quote=True)
            pills.append(
                f'<a class="link-pill" href="{safe_u}" target="_blank" rel="noopener noreferrer">'
                f"🔗 {safe_d}</a>"
            )
        pills_section = (
            '<section class="sources"><h3>参考链接</h3>'
            f'<div class="pill-row">{"".join(pills)}</div></section>'
        )

    body_html = "\n".join(blocks_html) if blocks_html else f"<p>{html.escape(body or '', quote=True)}</p>"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>{safe_title}</title>
  <style>
    :root {{
      --bg: #f5f5f7;
      --card: #ffffff;
      --text: #1d1d1f;
      --muted: #6e6e73;
      --accent: #0066cc;
      --pill-bg: #eef4ff;
      --border: #e5e5ea;
      --code-bg: #f2f2f7;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #000000;
        --card: #1c1c1e;
        --text: #f5f5f7;
        --muted: #98989d;
        --accent: #4da3ff;
        --pill-bg: #1f2a3d;
        --border: #2c2c2e;
        --code-bg: #2c2c2e;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 16px 14px 36px;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Helvetica Neue", sans-serif;
      font-size: 16px;
      line-height: 1.65;
      -webkit-font-smoothing: antialiased;
    }}
    .card {{
      max-width: 680px;
      margin: 0 auto;
      background: var(--card);
      border-radius: 18px;
      padding: 22px 20px;
      border: 1px solid var(--border);
      box-shadow: 0 6px 24px rgba(0, 0, 0, 0.05);
    }}
    .badge {{
      display: inline-block;
      font-size: 12px;
      font-weight: 600;
      color: var(--accent);
      background: var(--pill-bg);
      padding: 3px 10px;
      border-radius: 999px;
      margin-bottom: 10px;
    }}
    h1 {{
      font-size: 22px;
      line-height: 1.35;
      margin: 0 0 6px;
      letter-spacing: -0.01em;
    }}
    .meta {{
      font-size: 13px;
      color: var(--muted);
      margin-bottom: 18px;
      padding-bottom: 14px;
      border-bottom: 1px solid var(--border);
    }}
    h2 {{ font-size: 18px; margin: 20px 0 8px; }}
    h3 {{ font-size: 16px; margin: 16px 0 6px; }}
    p {{ margin: 10px 0; word-break: break-word; }}
    ul, ol {{ margin: 10px 0; padding-left: 22px; }}
    li {{ margin: 6px 0; }}
    a {{ color: var(--accent); text-decoration: none; word-break: break-all; }}
    a:hover {{ text-decoration: underline; }}
    code {{
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 14px;
      background: var(--code-bg);
      padding: 2px 6px;
      border-radius: 6px;
    }}
    pre {{
      background: var(--code-bg);
      padding: 12px 14px;
      border-radius: 12px;
      overflow-x: auto;
    }}
    pre code {{ padding: 0; background: transparent; }}
    blockquote {{
      margin: 12px 0;
      padding: 8px 14px;
      border-left: 3px solid var(--accent);
      background: var(--code-bg);
      border-radius: 0 10px 10px 0;
      color: var(--muted);
    }}
    hr {{ border: none; border-top: 1px solid var(--border); margin: 18px 0; }}
    .table-wrap {{ overflow-x: auto; margin: 14px 0; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
    th, td {{
      text-align: left;
      padding: 9px 10px;
      border-bottom: 1px solid var(--border);
    }}
    th {{ font-weight: 600; color: var(--muted); background: var(--code-bg); }}
    .sources {{
      margin-top: 22px;
      padding-top: 14px;
      border-top: 1px solid var(--border);
    }}
    .pill-row {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }}
    .link-pill {{
      display: inline-flex;
      align-items: center;
      padding: 6px 12px;
      border-radius: 999px;
      background: var(--pill-bg);
      color: var(--accent);
      font-size: 13px;
      font-weight: 500;
    }}
  </style>
</head>
<body>
  <article class="card">
    <div class="badge">小纳整理 · 手机阅读卡片</div>
    <h1>{safe_title}</h1>
    <div class="meta">生成时间：{timestamp_str}</div>
    {body_html}
    {pills_section}
  </article>
</body>
</html>
"""


def sanitize_attachment_filename(raw_name: str, default_stem: str = "小纳附件", ext: str = "") -> str:
    """Produce a clean, readable filename with zero path traversal or hidden dotfile characters."""
    cleaned = re.sub(r"[/\\\x00-\x1f<>:\"|?*]+", "_", (raw_name or "").strip())
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._ ")
    if not cleaned:
        cleaned = default_stem
    if ext:
        norm_ext = ext if ext.startswith(".") else f".{ext}"
        if not cleaned.lower().endswith(norm_ext.lower()):
            cleaned = f"{cleaned}{norm_ext}"
    return cleaned[:120]


_SENSITIVE_FILENAME_PATTERNS = (
    ".env",
    ".env.*",
    "*.db",
    "*.db-*",
    "*.sqlite",
    "*.sqlite3",
    "*.sqlite*",
    "chat.db",
    "*key*",
    "*secret*",
    "*token*",
    "*credential*",
    "*password*",
    "*.pem",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
)


def is_sensitive_attachment_path(file_path: str | Path) -> tuple[bool, str]:
    """Return (is_blocked, reason) to prevent accidental or prompt-injected secret exfiltration."""
    expanded = pathlib.Path(file_path).expanduser()
    candidates = [expanded]
    try:
        resolved = expanded.resolve()
        if resolved != expanded:
            candidates.append(resolved)
    except OSError:
        pass

    home = pathlib.Path.home()
    forbidden_roots = [
        pathlib.Path("/etc"),
        pathlib.Path("/private/etc"),
        pathlib.Path("/var/db"),
        home / ".ssh",
        home / ".gnupg",
        home / ".aws",
        home / ".yoooclaw",
        home / "Library" / "Messages",
        home / "Library" / "Keychains",
    ]

    for cand in candidates:
        low_name = cand.name.lower()
        for pat in _SENSITIVE_FILENAME_PATTERNS:
            if fnmatch.fnmatch(low_name, pat):
                return True, f"文件名命中敏感保护规则 ({pat})"

        for root in forbidden_roots:
            try:
                cand.relative_to(root)
                return True, f"路径位于受保护系统/密钥目录 ({root.name})"
            except ValueError:
                pass
            try:
                cand.relative_to(root.resolve())
                return True, f"路径位于受保护系统/密钥目录 ({root.name})"
            except (OSError, ValueError):
                pass

    return False, ""
