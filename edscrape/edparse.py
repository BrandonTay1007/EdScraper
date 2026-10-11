"""Ed content XML (``<document version="2.0">``) to Markdown and plain text.

Pure function over strings; no I/O. See ``parse``.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from edscrape.models import ParsedContent

_BLOCKS = frozenset(
    {
        "paragraph",
        "heading",
        "blockquote",
        "callout",
        "list",
        "figure",
        "file",
        "snippet",
        "web-snippet",
    }
)
_KNOWN = _BLOCKS | frozenset(
    {
        "document",
        "break",
        "bold",
        "italic",
        "underline",
        "strike",
        "code",
        "link",
        "list-item",
        "image",
        "snippet-file",
        "web-snippet-file",
        "mention",
    }
)
_MALFORMED = "<malformed>"


class _Renderer:
    """Renders one element tree in either markdown or plain-text mode."""

    def __init__(self, md: bool) -> None:
        self.md = md

    # -- inline ---------------------------------------------------------
    def inline(self, el: ET.Element) -> str:
        out = [el.text or ""]
        for child in el:
            out.append(self.element(child))
            out.append(child.tail or "")
        return "".join(out)

    def wrap(self, el: ET.Element, mark: str) -> str:
        inner = self.inline(el)
        if not self.md or not inner.strip():
            return inner
        return f"{mark}{inner}{mark}"

    def element(self, el: ET.Element) -> str:
        tag = el.tag
        if tag == "break":
            return "\n"
        if tag == "bold":
            return self.wrap(el, "**")
        if tag == "italic":
            return self.wrap(el, "*")
        if tag == "strike":
            return self.wrap(el, "~~")
        if tag == "code":
            inner = self.inline(el)
            if not self.md or not inner:
                return inner
            fence = "``" if "`" in inner else "`"
            return f"{fence}{inner}{fence}"
        if tag == "link":
            inner = self.inline(el)
            href = el.get("href")
            if self.md and href:
                return f"[{inner or href}]({href})"
            return inner
        if tag == "image":
            return f"![image]({el.get('src', '')})" if self.md else "[image]"
        if tag in _BLOCKS:
            return self.block(el)
        # underline, mention, unknown tags: inner text only
        return self.inline(el)

    # -- blocks ---------------------------------------------------------
    def container(self, el: ET.Element, sep: str = "\n\n") -> str:
        """Render block children; loose inline runs become their own parts."""
        parts: list[str] = []
        buf = [el.text or ""]

        def flush() -> None:
            s = "".join(buf)
            if s.strip():
                parts.append(s.strip())
            buf.clear()

        for child in el:
            if child.tag in _BLOCKS:
                flush()
                rendered = self.block(child)
                if rendered:
                    parts.append(rendered)
                buf.append(child.tail or "")
            else:
                buf.append(self.element(child))
                buf.append(child.tail or "")
        flush()
        return sep.join(parts)

    def block(self, el: ET.Element) -> str:
        tag = el.tag
        if tag == "paragraph":
            return self.inline(el).strip()
        if tag == "heading":
            inner = self.inline(el).strip()
            if not self.md:
                return inner
            try:
                level = min(max(int(el.get("level", "1")), 1), 6)
            except ValueError:
                level = 1
            return f"{'#' * level} {inner}"
        if tag == "blockquote":
            inner = self.container(el)
            return self.quote(inner) if self.md else inner
        if tag == "callout":
            inner = self.container(el)
            if not self.md:
                return inner
            return self.quote(f"**{el.get('type', 'info')}:** {inner}")
        if tag == "list":
            return self.list_(el)
        if tag == "figure":
            return "\n\n".join(
                r for r in (self.element(c) for c in el if c.tag == "image") if r
            )
        if tag == "file":
            url = el.get("url", "")
            name = el.get("filename") or url
            if self.md:
                return f"[{name}]({url})" if url else name
            return f"[file: {name}]"
        if tag == "snippet":
            files = [c for c in el if c.tag == "snippet-file"]
            code = "\n".join(self.raw(f) for f in files) if files else self.raw(el)
            return self.fence(code, el.get("language", ""))
        if tag == "web-snippet":
            files = [c for c in el if c.tag == "web-snippet-file"]
            if not files:
                return self.fence(self.raw(el), "")
            if not self.md:
                return "\n".join(self.raw(f) for f in files)
            return "\n\n".join(self.fence(self.raw(f), f.get("id", "")) for f in files)
        return self.inline(el)  # unreachable: every _BLOCKS tag is handled above

    @staticmethod
    def raw(el: ET.Element) -> str:
        return "".join(el.itertext()).strip("\n")

    def fence(self, code: str, lang: str) -> str:
        if not self.md:
            return code
        return f"```{lang}\n{code}\n```"

    @staticmethod
    def quote(s: str) -> str:
        return "\n".join(f"> {line}" if line else ">" for line in s.split("\n"))

    def list_(self, el: ET.Element) -> str:
        numbered = el.get("style") == "number"
        lines: list[str] = []
        n = 0
        for item in el:
            if item.tag != "list-item":
                continue
            n += 1
            marker = f"{n}. " if numbered else "- "
            pad = " " * len(marker)
            body = self.container(item, sep="\n")
            first, *rest = body.split("\n")
            lines.append(f"{marker}{first}".rstrip())
            lines.extend(f"{pad}{r}" if r else "" for r in rest)
        return "\n".join(lines)


def _collect_unknown(root: ET.Element) -> list[str]:
    seen: list[str] = []
    for el in root.iter():
        if el.tag not in _KNOWN and el.tag not in seen:
            seen.append(el.tag)
    return seen


def parse(content_xml: str, fallback_text: str) -> ParsedContent:
    """Convert Ed ``content`` XML to Markdown and plain text.

    Empty input gives empty strings. Malformed XML or a root that is not
    ``<document>`` gives the fallback text with ``unknown_tags == ["<malformed>"]``.
    """
    if not content_xml or not content_xml.strip():
        return ParsedContent(markdown="", text="", unknown_tags=[])
    try:
        root = ET.fromstring(content_xml.strip())
    except ET.ParseError:
        return ParsedContent(fallback_text, fallback_text, [_MALFORMED])
    if root.tag != "document":
        return ParsedContent(fallback_text, fallback_text, [_MALFORMED])
    return ParsedContent(
        markdown=_Renderer(True).container(root).strip("\n").rstrip(),
        text=_Renderer(False).container(root).strip("\n").rstrip(),
        unknown_tags=_collect_unknown(root),
    )
