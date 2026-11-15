"""XiaoNa SMS Readability Formatter, Standalone Link Splitter, 3-Mode Safe HTML Renderer & Attachment Guards."""

from __future__ import annotations

import datetime as dt
import fnmatch
import html
from html.parser import HTMLParser
import pathlib
import re
from pathlib import Path

# Markdown link: [Title](https://example.com/path(with_parens)) or ![Alt](https://...)
_MD_LINK_RE = re.compile(
    r"!?\[([^\[\]\n]*)\]\(\s*<?(https?://(?:[^\s()<>]+|\([^\s()<>]*\))+)?>?\s*\)"
)
_MD_ANY_LINK_RE = re.compile(r"!?\[([^\[\]\n]*)\]\(\s*([^)\n]+?)\s*\)")
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
_PURE_URL_LINE_RE = re.compile(r"^https?://[^\s<>\"'`]+$")

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

            before_clean = before.rstrip()
            had_open_wrapper = False
            while before_clean and before_clean[-1] in _LEADING_GLUED_WRAPPERS:
                before_clean = before_clean[:-1].rstrip()
                had_open_wrapper = True

            if before_clean:
                segments.append(before_clean)
            segments.append(clean_url)

            if had_open_wrapper and trailing and trailing[0] in ")]>）】》」』”’":
                trailing = trailing[1:]
            cursor = end
            if trailing:
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

    # 2. Unpack Markdown links [Title](https://...) BEFORE table/inline processing
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

    # 6. Restore protected URLs and isolate each URL onto its own clean line
    for idx, url_val in enumerate(protected_urls):
        s = s.replace(f"@@INSTINCTURL{idx}@@", url_val)

    s = _isolate_standalone_urls(s)

    # 7. Normalize trailing spaces and blank lines
    cleaned_lines = [ln.rstrip() for ln in s.split("\n")]
    s = "\n".join(cleaned_lines)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def split_reply_and_standalone_links(
    formatted_text: str, max_links: int = 3
) -> tuple[str, list[str]]:
    """Split formatted SMS reply into (main_text_without_bare_url_lines, standalone_urls[:max_links])."""
    if not formatted_text or not formatted_text.strip():
        return "", []

    cap = max(0, int(max_links))
    if cap == 0:
        return formatted_text.strip(), []

    lines = formatted_text.replace("\r\n", "\n").split("\n")
    extracted: list[str] = []
    seen_urls: set[str] = set()
    kept_lines: list[str] = []

    # Identify trailing bare URL block indices so trailing excess URLs never clutter main_text
    trailing_url_indices: set[int] = set()
    idx_rev = len(lines) - 1
    while idx_rev >= 0:
        s_rev = lines[idx_rev].strip()
        if not s_rev:
            idx_rev -= 1
            continue
        if _PURE_URL_LINE_RE.fullmatch(s_rev):
            trailing_url_indices.add(idx_rev)
            idx_rev -= 1
            continue
        break

    for idx_line, line in enumerate(lines):
        stripped = line.strip()
        if _PURE_URL_LINE_RE.fullmatch(stripped):
            clean_url, _ = _clean_url_trailing_punct(stripped)
            if clean_url not in seen_urls and len(extracted) < cap:
                seen_urls.add(clean_url)
                extracted.append(clean_url)
                continue
            if clean_url in seen_urls or idx_line in trailing_url_indices:
                continue
        kept_lines.append(line)

    main_text = "\n".join(kept_lines)
    main_text = re.sub(r"\n{3,}", "\n\n", main_text).strip()
    return main_text, extracted


# ============================================================================
# 3-Mode Safe HTML Card Renderer (Mode A: Full Doc, Mode B/C: Fragment + Markdown)
# ============================================================================

_ALLOWED_HTML_TAGS = {
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "div", "span", "section", "article", "header", "footer", "main", "aside", "nav",
    "figure", "figcaption",
    "b", "strong", "i", "em", "u", "s", "del", "ins", "mark", "small", "sub", "sup", "abbr", "time",
    "ul", "ol", "li", "dl", "dt", "dd",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "colgroup", "col",
    "blockquote", "pre", "code", "br", "hr",
    "details", "summary", "a", "img", "style",
}

_VOID_HTML_TAGS = {"br", "hr", "img", "col"}
_DANGEROUS_CONTAINER_TAGS = {"script", "iframe", "object", "embed", "applet", "form"}
_BLOCK_START_TAG_RE = re.compile(
    r"^\s*</?(?:h[1-6]|p|div|section|article|header|footer|main|aside|nav|ul|ol|li|dl|dt|dd|"
    r"table|thead|tbody|tfoot|tr|th|td|blockquote|pre|hr|details|summary|figure|figcaption|style)\b",
    re.IGNORECASE,
)

_SAFE_GLOBAL_ATTRS = {
    "class", "id", "style", "title", "role", "lang", "dir",
    "colspan", "rowspan", "align", "valign", "width", "height", "open",
}


def _is_safe_href(url: str) -> bool:
    u = (url or "").strip().lower()
    if not u:
        return False
    return u.startswith(("https://", "http://", "mailto:", "tel:", "#"))


def _is_safe_img_src(url: str) -> bool:
    u = (url or "").strip().lower()
    if not u:
        return False
    return u.startswith(("https://", "http://", "data:image/"))


def _sanitize_css_text(css: str) -> str:
    cleaned = re.sub(r"(?i)expression\s*\([^)]*\)", "", css)
    cleaned = re.sub(r"(?i)javascript\s*:", "", cleaned)
    cleaned = re.sub(r"(?i)vbscript\s*:", "", cleaned)
    cleaned = re.sub(r"(?i)@import\b[^;]*;?", "", cleaned)
    cleaned = re.sub(r"(?i)</style", "", cleaned)
    return cleaned


def _sanitize_full_html_document(raw_html: str) -> str:
    """Mode A: Sanitize ONLY active XSS vectors while preserving 100% of custom layout/CSS/design."""
    doc = raw_html
    for tag in ("script", "iframe", "object", "embed", "applet"):
        doc = re.sub(rf"(?is)<{tag}\b[^>]*>.*?</{tag}>", "", doc)
        doc = re.sub(rf"(?is)<{tag}\b[^>]*/?>", "", doc)
    doc = re.sub(r"(?is)<base\b[^>]*/?>", "", doc)

    # Strip inline event handlers like onclick=..., onerror=...
    doc = re.sub(
        r"""(?i)\s+on[a-z0-9_]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>"']+)""",
        "",
        doc,
    )
    # Neutralize javascript:/vbscript:/data:text/html in href/src/action
    doc = re.sub(
        r"""(?i)\b(href|src|action|formaction)\s*=\s*(["'])\s*(?:javascript|vbscript|data\s*:\s*text/html)[^"']*\2""",
        r'\1="#"',
        doc,
    )
    doc = re.sub(
        r"""(?i)\b(href|src|action|formaction)\s*=\s*(?:javascript|vbscript|data:text/html)[^\s>]*""",
        r'\1="#"',
        doc,
    )

    low = doc.lower()
    need_charset = 'charset=' not in low
    need_viewport = 'name="viewport"' not in low and "name='viewport'" not in low
    inject_meta = ""
    if need_charset:
        inject_meta += '\n  <meta charset="utf-8">'
    if need_viewport:
        inject_meta += '\n  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'

    if inject_meta:
        head_m = re.search(r"(?i)<head\b[^>]*>", doc)
        if head_m:
            pos = head_m.end()
            doc = doc[:pos] + inject_meta + doc[pos:]
        else:
            html_m = re.search(r"(?i)<html\b[^>]*>", doc)
            if html_m:
                pos = html_m.end()
                doc = doc[:pos] + f"\n<head>{inject_meta}\n</head>" + doc[pos:]
            else:
                doc = f"<head>{inject_meta}\n</head>\n" + doc
    return doc


def _render_inline_md_in_text_node(raw: str, discovered_urls: list[str], inside_anchor: bool) -> str:
    """Convert Markdown links/bare URLs/**bold**/``code`` inside plain text nodes while escaping HTML."""
    tokens: list[str] = []

    def _save_tok(frag: str) -> str:
        key = f"@@XNTK{len(tokens)}@@"
        tokens.append(frag)
        return key

    work = raw

    if not inside_anchor:
        def _any_md_link(m: re.Match[str]) -> str:
            label = (m.group(1) or "").strip()
            raw_target = (m.group(2) or "").strip().strip("<>")
            url, _ = _clean_url_trailing_punct(raw_target)
            if not (url.startswith("http://") or url.startswith("https://")):
                return html.escape(label or m.group(0), quote=True)
            if url not in discovered_urls:
                discovered_urls.append(url)
            sh = html.escape(url, quote=True)
            sl = html.escape(label or url, quote=True)
            return _save_tok(
                f'<a href="{sh}" target="_blank" rel="noopener noreferrer">{sl}</a>'
            )

        work = _MD_ANY_LINK_RE.sub(_any_md_link, work)

        def _bare_url(m: re.Match[str]) -> str:
            url, suffix = _clean_url_trailing_punct(m.group(0))
            if url not in discovered_urls:
                discovered_urls.append(url)
            sh = html.escape(url, quote=True)
            tok = _save_tok(
                f'<a href="{sh}" target="_blank" rel="noopener noreferrer">{sh}</a>'
            )
            return tok + html.escape(suffix, quote=True)

        work = _BARE_URL_TOKEN_RE.sub(_bare_url, work)

    escaped = html.escape(work, quote=True)
    escaped = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = _ITALIC_STAR_RE.sub(r"<em>\1</em>", escaped)

    for idx, frag in enumerate(tokens):
        escaped = escaped.replace(f"@@XNTK{idx}@@", frag)
    return escaped


class _HybridHTMLFragmentSanitizer(HTMLParser):
    """Sanitizes HTML fragments via stdlib HTMLParser, preserving safe tags/classes/styles & rendering inline Markdown."""

    def __init__(self, discovered_urls: list[str]) -> None:
        super().__init__(convert_charrefs=True)
        self.discovered_urls = discovered_urls
        self.out: list[str] = []
        self._drop_stack: list[str] = []
        self._open_tags: list[str] = []
        self.has_h1 = False

    def _in_raw_code_or_style(self) -> str | None:
        for t in reversed(self._open_tags):
            if t in ("pre", "code", "style"):
                return t
        return None

    def _in_anchor(self) -> bool:
        return "a" in self._open_tags

    def _sanitize_attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> list[tuple[str, str]] | None:
        clean: list[tuple[str, str]] = []
        attr_map = {k.lower(): (v if v is not None else "") for k, v in attrs if k}

        if tag == "img":
            src_val = attr_map.get("src", "").strip()
            if not _is_safe_img_src(src_val):
                return None

        for raw_k, raw_v in attrs:
            if not raw_k:
                continue
            k = raw_k.lower()
            if k.startswith("on"):
                continue
            v = raw_v if raw_v is not None else ""

            if k == "style":
                v_clean = _sanitize_css_text(v).strip()
                if v_clean:
                    clean.append(("style", v_clean))
                continue

            if tag == "a" and k == "href":
                v_trim = v.strip()
                if _is_safe_href(v_trim):
                    if v_trim.startswith(("http://", "https://")) and v_trim not in self.discovered_urls:
                        self.discovered_urls.append(v_trim)
                    clean.append(("href", v_trim))
                continue

            if tag == "img" and k in ("src", "alt", "width", "height", "title", "class", "id", "style"):
                clean.append((k, v.strip()))
                continue

            if k in _SAFE_GLOBAL_ATTRS:
                clean.append((k, v.strip()))

        if tag == "a":
            has_href = any(k == "href" for k, _ in clean)
            if has_href:
                clean.append(("target", "_blank"))
                clean.append(("rel", "noopener noreferrer"))

        return clean

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if self._drop_stack:
            if t in _DANGEROUS_CONTAINER_TAGS:
                self._drop_stack.append(t)
            return

        if t in _DANGEROUS_CONTAINER_TAGS:
            self._drop_stack.append(t)
            return

        if t not in _ALLOWED_HTML_TAGS:
            return

        sanitized = self._sanitize_attrs(t, attrs)
        if sanitized is None:
            return

        if t == "h1":
            self.has_h1 = True

        attr_str = "".join(f' {k}="{html.escape(v, quote=True)}"' for k, v in sanitized)
        self.out.append(f"<{t}{attr_str}>")
        if t not in _VOID_HTML_TAGS:
            self._open_tags.append(t)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if self._drop_stack or t in _DANGEROUS_CONTAINER_TAGS or t not in _ALLOWED_HTML_TAGS:
            return
        sanitized = self._sanitize_attrs(t, attrs)
        if sanitized is None:
            return
        attr_str = "".join(f' {k}="{html.escape(v, quote=True)}"' for k, v in sanitized)
        self.out.append(f"<{t}{attr_str}>")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if self._drop_stack:
            if self._drop_stack[-1] == t:
                self._drop_stack.pop()
            elif t in self._drop_stack:
                while self._drop_stack and self._drop_stack[-1] != t:
                    self._drop_stack.pop()
                if self._drop_stack:
                    self._drop_stack.pop()
            return

        if t not in _ALLOWED_HTML_TAGS or t in _VOID_HTML_TAGS:
            return

        if t in self._open_tags:
            while self._open_tags:
                top = self._open_tags.pop()
                self.out.append(f"</{top}>")
                if top == t:
                    break

    def handle_data(self, data: str) -> None:
        if self._drop_stack or not data:
            return
        ctx = self._in_raw_code_or_style()
        if ctx == "style":
            self.out.append(_sanitize_css_text(data))
        elif ctx in ("pre", "code"):
            self.out.append(html.escape(data, quote=False))
        else:
            self.out.append(
                _render_inline_md_in_text_node(
                    data, self.discovered_urls, inside_anchor=self._in_anchor()
                )
            )

    def handle_entityref(self, name: str) -> None:
        if not self._drop_stack:
            self.out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self._drop_stack:
            self.out.append(f"&#{name};")

    def finish(self) -> str:
        while self._open_tags:
            top = self._open_tags.pop()
            self.out.append(f"</{top}>")
        return "".join(self.out)


def _preprocess_markdown_blocks_into_html(body: str) -> str:
    """Convert Markdown block constructs (fenced code, pipe tables, # headings, lists, > quotes) while leaving HTML blocks intact."""
    norm = (body or "").replace("\r\n", "\n").replace("\r", "\n")

    # 1. Stash fenced code blocks so inner text is never parsed as HTML tags
    code_stash: list[str] = []

    def _stash_fenced(m: re.Match[str]) -> str:
        idx = len(code_stash)
        escaped_code = html.escape(m.group(1).strip("\n"), quote=False)
        code_stash.append(f"<pre><code>{escaped_code}</code></pre>")
        return f"\n@@XNCODEBLOCK{idx}@@\n"

    norm = _FENCED_CODE_RE.sub(_stash_fenced, norm)

    lines = norm.split("\n")
    out_blocks: list[str] = []
    i = 0
    n = len(lines)
    in_style_or_table = 0

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        m_code = re.fullmatch(r"@@XNCODEBLOCK(\d+)@@", stripped)
        if m_code:
            out_blocks.append(code_stash[int(m_code.group(1))])
            i += 1
            continue

        # Track multi-line raw <style> or <table> blocks so lines inside aren't wrapped in <p>
        if re.match(r"(?i)^<(?:style|table|ul|ol|dl|blockquote|pre|div|section|article|header|footer|details)\b", stripped):
            raw_chunk: list[str] = [line]
            i += 1
            while i < n and lines[i].strip():
                raw_chunk.append(lines[i])
                i += 1
            out_blocks.append("\n".join(raw_chunk))
            continue

        if _BLOCK_START_TAG_RE.match(stripped):
            out_blocks.append(line)
            i += 1
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
                thead = "".join(f"<th>{c}</th>" for c in hdr_cells)
                tbody_rows: list[str] = []
                for dr in data_rows[1:]:
                    cells = [c.strip() for c in dr.strip().strip("|").split("|")]
                    tds = "".join(f"<td>{c}</td>" for c in cells)
                    tbody_rows.append(f"<tr>{tds}</tr>")
                out_blocks.append(
                    f'<div class="table-wrap"><table><thead><tr>{thead}</tr></thead>'
                    f"<tbody>{''.join(tbody_rows)}</tbody></table></div>"
                )
            continue

        # Horizontal rule
        if _HR_RE.match(stripped):
            out_blocks.append("<hr>")
            i += 1
            continue

        # Markdown heading
        hm = re.match(r"^(#{1,6})\s+(.+?)\s*#*$", stripped)
        if hm:
            lvl = min(4, max(2, len(hm.group(1)) + 1))
            out_blocks.append(f"<h{lvl}>{hm.group(2)}</h{lvl}>")
            i += 1
            continue

        # Markdown unordered list
        if re.match(r"^[-*+·]\s+", stripped):
            items: list[str] = []
            while i < n and re.match(r"^[-*+·]\s+", lines[i].strip()):
                item_txt = re.sub(r"^[-*+·]\s+", "", lines[i].strip(), count=1)
                items.append(f"<li>{item_txt}</li>")
                i += 1
            out_blocks.append(f"<ul>{''.join(items)}</ul>")
            continue

        # Markdown ordered list
        if re.match(r"^\d+[.)、]\s+", stripped):
            items = []
            while i < n and re.match(r"^\d+[.)、]\s+", lines[i].strip()):
                item_txt = re.sub(r"^\d+[.)、]\s+", "", lines[i].strip(), count=1)
                items.append(f"<li>{item_txt}</li>")
                i += 1
            out_blocks.append(f"<ol>{''.join(items)}</ol>")
            continue

        # Markdown blockquote (starts with '>' followed by space or non-tag text)
        if stripped.startswith(">"):
            q_lines: list[str] = []
            while i < n and lines[i].strip().startswith(">"):
                q_txt = re.sub(r"^>\s?", "", lines[i].strip(), count=1)
                q_lines.append(q_txt)
                i += 1
            out_blocks.append(f"<blockquote>{'<br>'.join(q_lines)}</blockquote>")
            continue

        # Plain paragraph lines (may contain inline HTML like <b>...</b> or <span>...</span>)
        p_lines: list[str] = []
        while i < n:
            cur = lines[i].strip()
            if (
                not cur
                or re.fullmatch(r"@@XNCODEBLOCK\d+@@", cur)
                or _BLOCK_START_TAG_RE.match(cur)
                or (cur.startswith("|") and cur.endswith("|") and cur.count("|") >= 2)
                or re.match(r"^#{1,6}\s+", cur)
                or re.match(r"^[-*+·]\s+", cur)
                or re.match(r"^\d+[.)、]\s+", cur)
                or cur.startswith(">")
                or _HR_RE.match(cur)
            ):
                break
            p_lines.append(lines[i].strip())
            i += 1
        if p_lines:
            out_blocks.append(f"<p>{'<br>'.join(p_lines)}</p>")

    _ = in_style_or_table
    return "\n".join(out_blocks)


def render_mobile_html_card(
    title: str,
    body: str,
    source_links: list[str] | None = None,
) -> str:
    """3-Mode Safe HTML Renderer:
    - Mode A: Full `<!DOCTYPE html>` / `<html>` custom document -> XSS-sanitized 100% custom layout passthrough.
    - Mode B & C: HTML fragments (`<h2>...</h2><p class="tip">...</p>`) & Markdown -> hybrid sanitized mobile HTML5 document.
    """
    raw_body = body or ""
    low_probe = raw_body.lstrip()[:256].lower()
    if "<!doctype html" in low_probe or "<html" in low_probe:
        return _sanitize_full_html_document(raw_body)

    safe_title = html.escape((title or "详情整理").strip(), quote=True)
    discovered_urls: list[str] = []

    hybrid_source = _preprocess_markdown_blocks_into_html(raw_body)
    safe_hybrid = re.sub(
        r"&(?!(?:[a-zA-Z][a-zA-Z0-9]*|#\d+|#x[0-9a-fA-F]+);)",
        "&amp;",
        hybrid_source,
    )
    sanitizer = _HybridHTMLFragmentSanitizer(discovered_urls=discovered_urls)
    sanitizer.feed(safe_hybrid)
    sanitizer.close()
    body_html = sanitizer.finish().strip()

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
            domain = domain_match.group(1) if domain_match else f"参考 {idx_l}"
            safe_u = html.escape(url, quote=True)
            safe_d = html.escape(domain, quote=True)
            pills.append(
                f'<a class="link-pill" href="{safe_u}" target="_blank" rel="noopener noreferrer">'
                f"{safe_d}</a>"
            )
        pills_section = (
            '<section class="sources"><h3>参考来源</h3>'
            f'<div class="pill-row">{"".join(pills)}</div></section>'
        )

    header_h1 = "" if sanitizer.has_h1 else f"    <h1>{safe_title}</h1>\n"
    if not body_html:
        body_html = f"<p>{html.escape(raw_body, quote=True)}</p>"

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
      --tip-bg: #f0f7ff;
      --tip-border: #3b82f6;
      --warn-bg: #fff8eb;
      --warn-border: #f59e0b;
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
        --tip-bg: #172336;
        --tip-border: #4da3ff;
        --warn-bg: #2b2110;
        --warn-border: #fbbf24;
        --border: #2c2c2e;
        --code-bg: #2c2c2e;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 18px 14px 38px;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Helvetica Neue", sans-serif;
      font-size: 16px;
      line-height: 1.68;
      -webkit-font-smoothing: antialiased;
    }}
    .card {{
      max-width: 700px;
      margin: 0 auto;
      background: var(--card);
      border-radius: 18px;
      padding: 24px 20px;
      border: 1px solid var(--border);
      box-shadow: 0 6px 24px rgba(0, 0, 0, 0.05);
    }}
    h1 {{
      font-size: 23px;
      line-height: 1.35;
      margin: 0 0 16px;
      padding-bottom: 12px;
      border-bottom: 1px solid var(--border);
      letter-spacing: -0.01em;
    }}
    h2 {{ font-size: 19px; margin: 22px 0 9px; line-height: 1.4; }}
    h3 {{ font-size: 16.5px; margin: 16px 0 6px; }}
    h4 {{ font-size: 15.5px; margin: 14px 0 4px; color: var(--muted); }}
    p {{ margin: 10px 0; word-break: break-word; }}
    ul, ol {{ margin: 10px 0; padding-left: 22px; }}
    li {{ margin: 6px 0; }}
    a {{ color: var(--accent); text-decoration: none; word-break: break-all; }}
    a:hover {{ text-decoration: underline; }}
    img {{ max-width: 100%; height: auto; border-radius: 12px; margin: 10px 0; }}
    .tip, .callout, .summary, .route {{
      margin: 12px 0;
      padding: 11px 14px;
      background: var(--tip-bg);
      border-left: 3.5px solid var(--tip-border);
      border-radius: 0 12px 12px 0;
      font-size: 15px;
    }}
    .warning, .caution {{
      margin: 12px 0;
      padding: 11px 14px;
      background: var(--warn-bg);
      border-left: 3.5px solid var(--warn-border);
      border-radius: 0 12px 12px 0;
      font-size: 15px;
    }}
    .tag, .badge, .highlight {{
      display: inline-block;
      font-size: 12.5px;
      font-weight: 600;
      color: var(--accent);
      background: var(--pill-bg);
      padding: 2px 9px;
      border-radius: 999px;
    }}
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
      padding: 9px 14px;
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
    @media print {{
      body {{ background: #ffffff; padding: 0; }}
      .card {{ box-shadow: none; border: none; max-width: 100%; padding: 8px 4px; }}
    }}
  </style>
</head>
<body>
  <article class="card">
{header_h1}    {body_html}
    {pills_section}
  </article>
</body>
</html>
"""


def sanitize_attachment_filename(raw_name: str, default_stem: str = "附件", ext: str = "") -> str:
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
