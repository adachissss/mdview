"""Markdown 渲染、代码高亮及 HTML 清理。"""
from __future__ import annotations

import html as htmllib
import posixpath
import re
import urllib.parse

import nh3
from markdown_it import MarkdownIt
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound


def e(text: str) -> str:
    return htmllib.escape(str(text), quote=True)


def q(text: str) -> str:
    """把仓库内相对路径变成安全 URL 片段。"""
    return urllib.parse.quote(text, safe="/")


# 中文排版的 CommonMark 兼容处理：
# `**本页操作：**第` 这种「闭合定界符紧跟在全角标点后」的写法，严格 CommonMark 认为
# 无法闭合，会原样显示星号。中文作者大量这么写，因此在定界符内侧补零宽空格，
# 渲染后再统一删掉，既不影响视觉也不改变正文内容。
_CJK_PUNCT = "，。：；！？、（）【】《》「」『』“”‘’…—·～"
_CJK_CLASS = "".join(re.escape(ch) for ch in _CJK_PUNCT)
_ZWSP = "\u200b"


def fix_cjk_emphasis(text: str) -> str:
    text = re.sub(rf"(?<=[{_CJK_CLASS}])(\*\*)", _ZWSP + r"\1", text)
    text = re.sub(rf"(\*\*)(?=[{_CJK_CLASS}])", r"\1" + _ZWSP, text)
    text = re.sub(rf"(?<!\*)(\*)(?=[{_CJK_CLASS}])", r"\1" + _ZWSP, text)
    text = re.sub(rf"(?<=[{_CJK_CLASS}])(?<!\*)(\*)(?!\*)", _ZWSP + r"\1", text)
    return text


def _render_fence(tokens, idx, options, env) -> str:
    token = tokens[idx]
    info = (token.info or "").strip()
    lang = info.split()[0].lstrip("{.").rstrip("}") if info else ""
    code = token.content
    if lang.lower() == "mermaid":
        return (
            '<div class="mermaid-wrap">'
            f'<pre class="mermaid">{e(code)}</pre>'
            "</div>\n"
        )
    if lang and lang.lower() not in ("text", "plain", "plaintext", "txt"):
        try:
            lexer = get_lexer_by_name(lang, stripall=False)
        except ClassNotFound:
            lexer = None
        if lexer is not None:
            body = highlight(code, lexer, HtmlFormatter(nowrap=True))
            cls = f' class="language-{e(lang)}"'
            return (
                f'<div class="code-block" data-lang="{e(lang)}">'
                f"<button class=\"copy-btn\" type=\"button\" aria-label=\"复制代码\">复制</button>"
                f"<pre><code{cls}>{body}</code></pre></div>\n"
            )
    return (
        f'<div class="code-block" data-lang="{e(lang or "text")}">'
        f"<button class=\"copy-btn\" type=\"button\" aria-label=\"复制代码\">复制</button>"
        f"<pre><code>{e(code)}</code></pre></div>\n"
    )


def make_parser() -> MarkdownIt:
    md = MarkdownIt(
        "js-default",
        options_update={"html": True, "breaks": False, "typographer": False, "linkify": False},
    )
    md.enable("table")
    md.enable("strikethrough")
    md.renderer.rules["fence"] = _render_fence
    return md


PARSER = make_parser()
PYGMENTS_LIGHT = HtmlFormatter(style="friendly").get_style_defs(".code-block")
PYGMENTS_DARK = HtmlFormatter(style="monokai").get_style_defs('[data-theme="dark"] .code-block')
PYGMENTS_CSS = (
    "/* light */\n"
    + PYGMENTS_LIGHT
    + "\n/* dark */\n"
    + PYGMENTS_DARK
    # 覆盖 pygments 自带的背景色，保持和页面主题一致
    + "\n.code-block{background:var(--code-bg);}\n"
    + '[data-theme="dark"] .code-block{background:var(--code-bg);}\n'
)

_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*:|//|#|/)")
_HTML_ATTRS = {tag: set(attrs) for tag, attrs in nh3.ALLOWED_ATTRIBUTES.items()}
_HTML_ATTRS.update({
    "*": {"class", "title"},
    "div": {"data-lang"},
    "button": {"type", "aria-label"},
    "th": _HTML_ATTRS["th"] | {"style"},
    "td": _HTML_ATTRS["td"] | {"style"},
    **{f"h{n}": {"id"} for n in range(1, 7)},
})


def _resolve(rel_dir: str, target: str) -> str | None:
    """把文档内的相对地址解析成站点内路径；越界返回 None。"""
    if not target or _SCHEME_RE.match(target):
        return None
    raw = urllib.parse.unquote(urllib.parse.urlsplit(target).path)
    if not raw:
        return None
    if raw.startswith("/") or "\x00" in raw or "\\" in raw:
        return None
    joined = posixpath.normpath(posixpath.join(rel_dir, raw))
    if any(part.startswith(".") for part in joined.split("/")):
        return None
    return joined


def _sanitize(body: str, rel_dir: str) -> str:
    def filter_attr(tag: str, attr: str, value: str) -> str | None:
        if attr == "id":
            return value if re.fullmatch(r"s\d+", value) else None
        if (tag, attr) not in (("a", "href"), ("img", "src")):
            return value
        if not value or _SCHEME_RE.match(value):
            return value
        try:
            resolved = _resolve(rel_dir, value)
            if resolved is None:
                return None
            parts = urllib.parse.urlsplit(value)
        except ValueError:
            return None
        prefix = "/d/" if tag == "a" and resolved.lower().endswith((".md", ".markdown")) else "/asset/"
        return urllib.parse.urlunsplit(("", "", prefix + q(resolved), parts.query, parts.fragment))

    return nh3.clean(
        body,
        tags=nh3.ALLOWED_TAGS | {"button"},
        attributes=_HTML_ATTRS,
        attribute_filter=filter_attr,
        clean_content_tags={"script", "style", "iframe", "object", "embed", "svg", "math"},
        url_schemes={"http", "https", "mailto"},
        filter_style_properties={"text-align"},
    )


_TASK_EMPTY_RE = re.compile(r"<li>(\s*)\[ \]\s+")
_TASK_DONE_RE = re.compile(r"<li>(\s*)\[[xX]\]\s+")


def _task_lists(body: str) -> str:
    """把 GitHub 风格的 - [ ] / - [x] 渲染成方框，而不是字面量。"""
    body = _TASK_EMPTY_RE.sub('<li class="task"><span class="box"></span>', body)
    body = _TASK_DONE_RE.sub('<li class="task done"><span class="box checked"></span>', body)
    return body


def render_markdown(text: str, rel_dir: str) -> dict:
    env: dict = {}
    tokens = PARSER.parse(fix_cjk_emphasis(text), env)
    toc: list[tuple[int, str, str]] = []
    counter = 0
    for i, token in enumerate(tokens):
        if token.type == "heading_open" and token.tag in ("h1", "h2", "h3", "h4"):
            counter += 1
            hid = f"s{counter}"
            token.attrSet("id", hid)
            txt = ""
            if i + 1 < len(tokens) and tokens[i + 1].type == "inline":
                inline = tokens[i + 1]
                if inline.children:
                    txt = "".join(
                        c.content for c in inline.children if c.type in ("text", "code_inline")
                    )
                else:
                    txt = inline.content
            toc.append((int(token.tag[1]), txt.strip(), hid))
    body = PARSER.renderer.render(tokens, PARSER.options, env)
    body = _sanitize(body, rel_dir)
    body = body.replace("<table>", '<div class="table-wrap"><table>').replace("</table>", "</table></div>")
    body = _task_lists(body)
    if _ZWSP in body:
        body = body.replace(_ZWSP, "")
    return {"html": body, "toc": toc}
