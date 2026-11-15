#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mdview —— 局域网 Markdown 阅读服务（单文件）

设计要点
  * 只暴露一个根目录（默认 ./docs），目录里的 .md 才会被看到，其它文档天然隔离。
  * 左侧可折叠目录树 + 右侧渲染，服务端渲染，平板友好。
  * HTTP Basic 口令 + 一次性 ?k= 换 cookie，局域网内有门槛。
  * 只读：没有任何写接口。

用法
  python3 serve.py                       # 默认 0.0.0.0:8899，根目录 ./docs
  python3 serve.py --port 8080
  python3 serve.py --root ~/阅读文档
  python3 serve.py --no-auth             # 局域网免密
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html as htmllib
import json
import mimetypes
import os
import posixpath
import re
import secrets
import socket
import stat
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from markdown_it import MarkdownIt
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name

VERSION = "1.4"
APP_DIR = Path(__file__).resolve().parent
AUTH_FILE = APP_DIR / "auth"          # 口令文件也放在项目里，整个项目自包含
COOKIE_NAME = "mdview_sess"
STATIC_DIR = APP_DIR / "static"


def asset_version() -> str:
    """静态资源指纹：前端文件任一改动都会变，避免平板上拿到旧脚本。"""
    parts = [VERSION]
    for f in (STATIC_DIR / "app.css", STATIC_DIR / "app.js", APP_DIR / "serve.py"):
        try:
            parts.append(str(int(f.stat().st_mtime)))
        except OSError:
            continue
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:8]


ASSET_VER = asset_version()

# ---------------------------------------------------------------------------
# 配置（main 里填充）
# ---------------------------------------------------------------------------
CFG = {
    "root": (APP_DIR / "docs").resolve(),
    # token: 打开带 ?k= 的链接即放行，浏览器弹不出口令框
    # basic: 传统 HTTP Basic 账号密码弹窗
    # none:  局域网免验证
    "auth_mode": "token",
    "auth_user": "md",
    "auth_token": "",
    "verbose": False,
}

# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def e(text: str) -> str:
    return htmllib.escape(str(text), quote=True)


def q(text: str) -> str:
    """把仓库内相对路径变成安全 URL 片段。"""
    return urllib.parse.quote(text, safe="/")


def human_size(num: int) -> str:
    for unit in ("B", "KB", "MB"):
        if num < 1024 or unit == "MB":
            return f"{num:.0f}{unit}" if unit == "B" else f"{num:.1f}{unit}"
        num /= 1024.0
    return f"{num:.0f}B"


def human_time(ts: float) -> str:
    delta = time.time() - ts
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    if delta < 86400 * 7:
        return f"{int(delta // 86400)} 天前"
    return time.strftime("%Y-%m-%d", time.localtime(ts))


# ---------------------------------------------------------------------------
# 文档扫描与缓存
# ---------------------------------------------------------------------------
_scan_lock = threading.Lock()
_scan_state = {"docs": [], "stamp": None, "checked": 0.0}
_meta_lock = threading.Lock()
_meta_cache: dict[str, tuple] = {}   # rel -> (mtime, size, title)
_render_cache: dict[str, tuple] = {}  # rel -> (mtime, size, payload)

SKIP_DIRS = {"node_modules", ".git", ".svn", "__pycache__", ".venv", "venv", ".idea", ".vscode"}

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


def iter_md_files(root: Path):
    """遍历根目录下的 .md / .pdf，跟随软链接（ln -s 进来的外部目录也能用）。

    用 (dev, inode) 去重，避免软链接互相指向造成死循环。
    """
    seen: set[tuple[int, int]] = set()
    stack: list[Path] = [root]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                entries = sorted(it, key=lambda e: e.name.casefold())
        except OSError:
            continue
        for ent in entries:
            name = ent.name
            if name.startswith(".") or name in SKIP_DIRS:
                continue
            try:
                st = ent.stat(follow_symlinks=True)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                key = (st.st_dev, st.st_ino)
                if key in seen:
                    continue
                seen.add(key)
                stack.append(Path(ent.path))
            elif stat.S_ISREG(st.st_mode):
                p = Path(ent.path)
                if p.suffix.lower() in (".md", ".markdown", ".pdf"):
                    yield p


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def extract_title(path: Path, rel: str) -> str:
    """取标题：优先 H1；没有 H1 就退到第一个任意级标题；再没有就用文件名。"""
    stem = Path(rel).stem
    try:
        head = path.read_bytes()[:8192].decode("utf-8", "replace")
    except OSError:
        return stem
    fallback = None
    for raw in head.lstrip("\ufeff").splitlines():
        m = _HEADING_RE.match(raw.strip())
        if not m:
            continue
        text = m.group(2).strip()
        if not text:
            continue
        if len(m.group(1)) == 1:
            return text
        if fallback is None:
            fallback = text
    return fallback or stem


def doc_meta(path: Path, rel: str) -> dict:
    st = path.stat()
    key = (st.st_mtime, st.st_size)
    with _meta_lock:
        cached = _meta_cache.get(rel)
        if cached and cached[0] == key:
            title = cached[1]
        else:
            title = extract_title(path, rel)
            _meta_cache[rel] = (key, title)
    stem = Path(rel).stem
    kind = "pdf" if Path(rel).suffix.lower() == ".pdf" else "md"
    return {
        "rel": rel,
        "name": stem,
        "label": stem,          # 侧栏一律显示文件名，标题只在悬停提示里
        "title": title if kind == "md" else stem,
        "kind": kind,
        "mtime": st.st_mtime,
        "size": st.st_size,
    }


def scan_docs() -> list[dict]:
    """扫描根目录。

    两层节流：1 秒内直接返回缓存（避免每次请求都遍历磁盘）；
    超过 1 秒再用 (文件数, 最大 mtime) 判断是否真的有变化。
    """
    root = CFG["root"]
    now = time.time()
    with _scan_lock:
        if _scan_state["docs"] and now - _scan_state["checked"] < 1.0:
            return _scan_state["docs"]
    files = list(iter_md_files(root)) if root.is_dir() else []
    stamp = (len(files), max((f.stat().st_mtime for f in files), default=0.0))
    with _scan_lock:
        if _scan_state["stamp"] == stamp and _scan_state["docs"]:
            _scan_state["checked"] = now
            return _scan_state["docs"]
    docs = []
    for f in files:
        rel = f.relative_to(root).as_posix()
        try:
            docs.append(doc_meta(f, rel))
        except OSError:
            continue
    docs.sort(key=lambda d: d["rel"].casefold())
    with _scan_lock:
        _scan_state["docs"] = docs
        _scan_state["stamp"] = stamp
        _scan_state["checked"] = now
    return docs


# ---------------------------------------------------------------------------
# Markdown 渲染
# ---------------------------------------------------------------------------


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
        except Exception:
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

_SCRIPT_RE = re.compile(r"<\s*(script|iframe|object|embed)\b[^>]*>.*?<\s*/\s*\1\s*>", re.I | re.S)
_ON_ATTR_RE = re.compile(r"\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)
_JS_URL_RE = re.compile(r"\b(href|src)\s*=\s*([\"'])\s*javascript:[^\"']*\2", re.I)
_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*:|//|#|/)")


def _resolve(rel_dir: str, target: str) -> str | None:
    """把文档内的相对地址解析成站点内路径；越界返回 None。"""
    if not target or _SCHEME_RE.match(target):
        return None
    raw = urllib.parse.unquote(target.split("#", 1)[0].split("?", 1)[0])
    if not raw:
        return None
    joined = posixpath.normpath(posixpath.join(rel_dir, raw))
    if joined.startswith("../") or joined == "..":
        return None
    return joined


def _rewrite_urls(body: str, rel_dir: str) -> str:
    def fix_img(m):
        prefix, src = m.group(1), m.group(2)
        resolved = _resolve(rel_dir, src)
        if resolved is None:
            return m.group(0)
        return f'{prefix}/asset/{q(resolved)}"'

    def fix_anchor(m):
        prefix, href = m.group(1), m.group(2)
        resolved = _resolve(rel_dir, href)
        if resolved is None:
            return m.group(0)
        if resolved.lower().endswith((".md", ".markdown")):
            return f'{prefix}/d/{q(resolved)}"'
        return f'{prefix}/asset/{q(resolved)}"'

    body = re.sub(r'(<img\b[^>]*?\bsrc=")([^"]*)"', fix_img, body)
    body = re.sub(r'(<a\b[^>]*?\bhref=")([^"]*)"', fix_anchor, body)
    body = body.replace("<table>", '<div class="table-wrap"><table>').replace("</table>", "</table></div>")
    return body


def _sanitize(body: str) -> str:
    body = _SCRIPT_RE.sub("", body)
    body = _ON_ATTR_RE.sub("", body)
    body = _JS_URL_RE.sub(r'\1="#"', body)
    return body


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
    body = _rewrite_urls(body, rel_dir)
    body = _sanitize(body)
    body = _task_lists(body)
    if _ZWSP in body:
        body = body.replace(_ZWSP, "")
    return {"html": body, "toc": toc}


def resolve_inside(rel: str) -> Path | None:
    """把请求路径按字面限制在根目录内。

    不做 realpath：docs 里的软链接允许指向外部目录（用户自己 ln -s 进来的素材），
    但仍然拒绝 .. 越级和隐藏文件，服务本身是只读的。
    """
    rel = posixpath.normpath("/" + rel).lstrip("/")
    if not rel or rel.startswith(".."):
        return None
    parts = rel.split("/")
    if any(not p or p in (".", "..") or p.startswith(".") for p in parts):
        return None
    return CFG["root"] / rel


def get_doc(rel: str) -> dict | None:
    target = resolve_inside(rel)
    if target is None or target.suffix.lower() not in (".md", ".markdown"):
        return None
    if not target.is_file():
        return None
    st = target.stat()
    with _meta_lock:
        cached = _render_cache.get(rel)
    if cached and cached[0] == (st.st_mtime, st.st_size):
        return cached[1]

    text = target.read_text(encoding="utf-8", errors="replace")
    rel_dir = posixpath.dirname(rel)
    rendered = render_markdown(text, rel_dir)
    payload = {
        "rel": rel,
        "title": extract_title(target, rel),
        "name": Path(rel).stem,
        "label": Path(rel).stem,
        "html": rendered["html"],
        "toc": rendered["toc"],
        "text": text,
        "mtime": st.st_mtime,
        "size": st.st_size,
    }
    with _meta_lock:
        _render_cache[rel] = ((st.st_mtime, st.st_size), payload)
    return payload


# ---------------------------------------------------------------------------
# 侧栏目录树
# ---------------------------------------------------------------------------


def build_tree(docs: list[dict]) -> dict:
    root: dict = {"name": "", "dirs": {}, "files": []}
    for doc in docs:
        parts = doc["rel"].split("/")
        node = root
        for seg in parts[:-1]:
            node = node["dirs"].setdefault(seg, {"name": seg, "dirs": {}, "files": []})
        node["files"].append(doc)
    return root


def render_tree(node: dict, current: str | None, depth: int = 0, prefix: str = "") -> str:
    out: list[str] = []
    for name, child in sorted(node["dirs"].items(), key=lambda kv: kv[0].casefold()):
        open_attr = " open" if depth < 1 else ""
        key = f"{prefix}/{name}" if prefix else name
        out.append(
            f'<li class="dir" data-dir="{e(name)}">'
            f'<details data-key="{e(key)}"{open_attr}><summary><span class="caret"></span>'
            f'<span class="dname">{e(name)}</span></summary>'
        )
        out.append(f'<ul class="tree">{render_tree(child, current, depth + 1, key)}</ul>')
        out.append("</details></li>")
    for doc in sorted(node["files"], key=lambda d: d["name"].casefold()):
        # 排序靠前端重排 DOM，这里把排序需要的数据带上
        data = (
            f'data-name="{e(doc["label"].casefold())}" '
            f'data-mtime="{doc["mtime"]:.3f}" data-size="{doc["size"]}"'
        )
        if doc.get("kind") == "pdf":
            out.append(
                '<li class="file pdf">'
                f'<a href="/rawpdf/{q(doc["rel"])}" target="_blank" rel="noopener" {data} '
                f'title="{e(doc["rel"])} · PDF（新标签打开）">'
                f'{e(doc["label"])}<span class="ext">PDF</span></a></li>'
            )
            continue
        active = " active" if doc["rel"] == current else ""
        out.append(
            f'<li class="file{active}">'
            f'<a href="/d/{q(doc["rel"])}" data-path="{e(doc["rel"])}" {data} '
            f'title="{e(doc["title"])}">{e(doc["label"])}</a></li>'
        )
    return "".join(out)


def sidebar_html(current: str | None, docs: list[dict]) -> str:
    if not docs:
        inner = '<p class="empty">docs/ 目录里还没有 Markdown 文件</p>'
    else:
        inner = f'<ul class="tree root">{render_tree(build_tree(docs), current)}</ul>'
    return (
        '<div class="side-head">'
        '<div class="brand">'
        f'<img class="brand-icon" src="/static/doc_on_doc.svg?v={ASSET_VER}" alt="" width="22" height="22" aria-hidden="true">'
        "<span>我的文档</span></div>"
        '<button class="icon-btn close-side" type="button" aria-label="收起目录">✕</button>'
        "</div>"
        '<div class="side-search">'
        '<input id="filter" type="search" placeholder="筛选文件 · 回车全文搜索" '
        'autocomplete="off" spellcheck="false">'
        '<div class="side-sort" role="group" aria-label="排序">'
        '<span class="sort-label">排序</span>'
        '<button type="button" class="sort-btn" data-sort="name-asc" title="名称 A→Z">A↓</button>'
        '<button type="button" class="sort-btn" data-sort="name-desc" title="名称 Z→A">A↑</button>'
        '<button type="button" class="sort-btn" data-sort="time-desc" title="最新在前"><svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="7" cy="7" r="4.7"/><path d="M7 4.4V7l1.9 1.2"/><path d="M12.6 10.4v3.6"/><path d="M11.2 12.6l1.4 1.4 1.4-1.4"/></svg></button>'
        '<button type="button" class="sort-btn" data-sort="time-asc" title="最早在前"><svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="7" cy="7" r="4.7"/><path d="M7 4.4V7l1.9 1.2"/><path d="M12.6 14v-3.6"/><path d="M11.2 11.8l1.4-1.4 1.4 1.4"/></svg></button>'
        '<button type="button" class="sort-btn" data-sort="size-desc" title="文件最大"><svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><path d="M3 4.5h10"/><path d="M3 8h7"/><path d="M3 11.5h4"/></svg></button>'
        '</div></div>'
        f'<nav class="side-tree" id="tree">{inner}</nav>'
        '<div class="side-foot">'
        f'<span>{len(docs)} 篇</span>'
        '<button type="button" class="foot-btn" id="add-dir" title="把某个目录以软链接加入 docs">＋ 目录</button>'
        '<a href="/search">全文搜索</a>'
        "</div>"
        '<div class="side-resizer" id="side-resizer" role="separator" aria-orientation="vertical"'
        ' aria-label="拖动调整目录宽度，双击恢复默认" title="拖动调宽，双击恢复默认"></div>'
    )


# ---------------------------------------------------------------------------
# 页面模板
# ---------------------------------------------------------------------------

PAGE_HEAD = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>{title}</title>
<link rel="icon" type="image/svg+xml" href="/static/doc_on_doc.svg?v={ver}">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png?v={ver}">
<link rel="stylesheet" href="/static/app.css?v={ver}">
<link rel="stylesheet" href="/static/pygments.css?v={ver}">
<link rel="stylesheet" href="/static/katex/katex.min.css?v={ver}">
<script>try{{
  var d=document.documentElement;
  var t=localStorage.getItem("mdview:theme");
  if(!t)t=matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light";
  d.dataset.theme=t;
  var z=parseInt(localStorage.getItem("mdview:zoom")||"100",10);
  if(z>=60&&z<=200){{
    d.style.setProperty("--zoom",String(z/100));
    d.style.setProperty("--doc-max",Math.round(736/(z/100))+"px");
  }}
  if(localStorage.getItem("mdview:sideCollapsed")==="1")d.classList.add("side-collapsed");
  var w=parseInt(localStorage.getItem("mdview:sideWidth")||"0",10);
  if(w>=90){{
    var lim=Math.max(120,Math.min(460,window.innerWidth-420));
    if(w>lim)w=lim;
    d.style.setProperty("--side-w",w+"px");
  }}
}}catch(e){{}}</script>
</head>
<body data-path="{path}">
<header class="topbar">
  <button class="icon-btn" id="menu" type="button" aria-label="目录">☰</button>
  <div class="crumbs">{crumbs}</div>
  <div class="tools">
    <div class="zoom" role="group" aria-label="内容缩放">
      <button class="icon-btn" id="zoom-out" type="button" aria-label="缩小">−</button>
      <button class="zoom-val" id="zoom-val" type="button" title="点击恢复 100%">100%</button>
      <button class="icon-btn" id="zoom-in" type="button" aria-label="放大">+</button>
    </div>
    <button class="icon-btn" id="theme" type="button" aria-label="切换深浅色">◐</button>
    <button class="icon-btn" id="fs" type="button" aria-label="全屏" title="全屏 / 退出全屏">⛶</button>
  </div>
</header>
<div class="overlay" id="overlay"></div>
<aside class="sidebar" id="sidebar">{sidebar}</aside>
<button class="side-tab" id="side-tab" type="button" aria-label="展开目录" title="展开目录">›</button>
<main class="content" id="content">
"""

PAGE_FOOT = """</main>
<div class="fs-bar" id="fs-bar" hidden>
  <button class="icon-btn" id="fs-zoom-out" type="button" aria-label="缩小">−</button>
  <button class="zoom-val" id="fs-zoom-val" type="button" title="恢复 100%">100%</button>
  <button class="icon-btn" id="fs-zoom-in" type="button" aria-label="放大">+</button>
  <span class="fs-sep"></span>
  <button class="fs-exit" id="fs-exit" type="button">退出全屏</button>
</div>
<button class="to-top" id="to-top" type="button" aria-label="回到顶部">↑</button>
<div class="lightbox" id="lightbox" aria-hidden="true"><img alt=""></div>
<script src="/static/katex/katex.min.js?v={ver}" defer></script>
<script src="/static/katex/contrib/auto-render.min.js?v={ver}" defer></script>
<script src="/static/app.js?v={ver}" defer></script>
</body>
</html>
"""


def page(title: str, sidebar: str, body: str, path: str, crumbs: str) -> str:
    return (
        PAGE_HEAD.format(title=e(title), sidebar=sidebar, path=e(path), crumbs=crumbs, ver=ASSET_VER)
        + body
        + PAGE_FOOT.format(ver=ASSET_VER)
    )


def crumbs_for(rel: str | None, title: str | None = None) -> str:
    if not rel:
        return '<span class="crumb-current">首页</span>'
    parts = rel.split("/")
    out = []
    for seg in parts[:-1]:
        out.append(f'<span class="crumb">{e(seg)}</span><span class="sep">/</span>')
    label = title or Path(rel).stem
    out.append(f'<span class="crumb-current">{e(label)}</span>')
    return "".join(out)


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------


LEGACY_AUTH_FILE = Path.home() / ".config" / "mdview" / "auth"  # 旧版本位置，仅用于自动迁移


def _read_auth(path: Path) -> tuple[str, str] | None:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            user, _, pwd = line.partition(":")
            if pwd:
                return user, pwd
    except OSError:
        pass
    return None


def load_or_create_auth() -> tuple[str, str]:
    """口令文件放在项目目录内，保持整个项目自包含。"""
    if AUTH_FILE.is_file():
        got = _read_auth(AUTH_FILE)
        if got:
            return got

    # 旧版本把口令写在 ~/.config/mdview/auth，自动搬进项目目录
    if LEGACY_AUTH_FILE.is_file():
        got = _read_auth(LEGACY_AUTH_FILE)
        if got:
            AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
            AUTH_FILE.write_text(
                "# mdview 访问口令，改这里即可：用户名:密码\n"
                f"# 由旧位置 {LEGACY_AUTH_FILE} 迁移而来\n{got[0]}:{got[1]}\n",
                encoding="utf-8",
            )
            AUTH_FILE.chmod(0o600)
            try:
                LEGACY_AUTH_FILE.unlink()
                LEGACY_AUTH_FILE.parent.rmdir()
            except OSError:
                pass
            return got

    user = "md"
    pwd = secrets.token_urlsafe(9)
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTH_FILE.write_text(
        "# mdview 访问口令，改这里即可：用户名:密码\n"
        f"# 本文件只在局域网内使用，不要提交到代码仓库或分享\n{user}:{pwd}\n",
        encoding="utf-8",
    )
    AUTH_FILE.chmod(0o600)
    return user, pwd


def session_token(pwd: str) -> str:
    return hmac.new(pwd.encode(), b"mdview-session-v1", hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = f"mdview/{VERSION}"
    protocol_version = "HTTP/1.1"

    # -- 基础设施 ---------------------------------------------------------
    def log_message(self, fmt, *args):
        if CFG["verbose"]:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, status: int, payload: dict):
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            {"Cache-Control": "no-store"},
        )

    def _html(self, status: int, markup: str, extra: dict | None = None):
        headers = {"Cache-Control": "no-store"}
        headers.update(extra or {})
        self._send(status, markup.encode("utf-8"), "text/html; charset=utf-8", headers)

    def _redirect(self, location: str, extra: dict | None = None):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _not_found(self, message: str = "没有找到这个页面"):
        docs = scan_docs()
        body = (
            '<article class="doc"><div class="doc-body">'
            f'<h1>404</h1><p>{e(message)}</p>'
            '<p><a href="/">回到阅读页</a></p></div></article>'
        )
        self._html(
            404,
            page("404 · mdview", sidebar_html(None, docs), body, "", crumbs_for(None)),
        )

    def _gate_page(self) -> None:
        """token 模式下的拦截页：不弹浏览器口令框，只给一个令牌输入框。"""
        body = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>需要访问令牌 · mdview</title>
<style>
  :root{color-scheme:light dark}
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       font-family:-apple-system,"PingFang SC","Noto Sans CJK SC",sans-serif;
       background:#fbfbfa;color:#1f2328;padding:24px}
  @media (prefers-color-scheme:dark){body{background:#14161a;color:#dfe3e8}}
  .card{width:100%;max-width:420px}
  h1{font-size:20px;margin:0 0 8px}
  p{color:#6b7280;line-height:1.7;margin:0 0 18px;font-size:14px}
  form{display:flex;gap:8px}
  input{flex:1;min-width:0;padding:11px 12px;font-size:15px;border-radius:9px;
        border:1px solid #d8d8d4;background:#fff;color:inherit}
  input:focus{outline:none;border-color:#1f6feb;box-shadow:0 0 0 3px rgba(31,111,235,.15)}
  button{padding:11px 18px;font-size:15px;border:0;border-radius:9px;background:#1f6feb;
         color:#fff;cursor:pointer}
  code{font-family:ui-monospace,Menlo,monospace;font-size:12.5px;background:#eee;
       padding:2px 5px;border-radius:4px}
  @media (prefers-color-scheme:dark){input{background:#20242b;border-color:#2a2f38}
                                     code{background:#262b33}}
</style></head>
<body><div class="card">
  <h1>mdview</h1>
  <p>这个地址缺少访问令牌。输入一次令牌，浏览器就会记住，之后直接打开地址即可。</p>
  <form method="get" action="__GATE_ACTION__">
    <input name="k" type="text" placeholder="访问令牌" autofocus autocomplete="off" spellcheck="false">
    <button type="submit">进入</button>
  </form>
  <p style="margin-top:16px">或者用带令牌的链接：<code>/?k=令牌</code></p>
</div></body></html>"""
        here = self.path.split("?", 1)[0] or "/"
        self._html(403, body.replace("__GATE_ACTION__", htmllib.escape(here, quote=True)))

    def _guard(self, query: dict) -> bool:
        mode = CFG["auth_mode"]
        if mode == "none":
            return True
        token = CFG["auth_token"]

        # 1) ?k=令牌：种一个长期 cookie，然后跳回不带令牌的干净地址
        key = (query.get("k") or [""])[0]
        if key and hmac.compare_digest(key, token):
            cookie = (
                f"{COOKIE_NAME}={session_token(token)}; Path=/; "
                "Max-Age=31536000; SameSite=Lax; HttpOnly"
            )
            clean = {k: v for k, v in query.items() if k != "k"}
            rest = urllib.parse.urlencode(clean, doseq=True)
            self._redirect(self.path.split("?", 1)[0] + (f"?{rest}" if rest else ""), {"Set-Cookie": cookie})
            return False

        # 2) cookie（浏览器记住之后走这里，全程无需输入）
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME and hmac.compare_digest(value, session_token(token)):
                return True

        # 3) 传统 Basic（仅 --basic 模式）
        if mode == "basic":
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Basic "):
                try:
                    decoded = base64.b64decode(auth[6:]).decode("utf-8", "replace")
                    user, _, given = decoded.partition(":")
                except Exception:
                    user, given = "", ""
                if hmac.compare_digest(user, CFG["auth_user"]) and hmac.compare_digest(given, token):
                    return True
                time.sleep(0.4)
            body = (
                "<!DOCTYPE html><meta charset=utf-8><title>需要口令</title>"
                "<body style='font-family:system-ui;padding:12vh 8vw;line-height:1.7'>"
                "<h2>需要访问口令</h2><p>这是一台局域网文档服务，请输入口令后继续。</p>"
                "</body>"
            )
            self._send(
                401,
                body.encode("utf-8"),
                "text/html; charset=utf-8",
                {"WWW-Authenticate": 'Basic realm="mdview", charset="UTF-8"', "Cache-Control": "no-store"},
            )
            return False

        # 4) token 模式：给拦截页，不弹窗
        self._gate_page()
        return False

    # -- 路由 -------------------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        raw_path = parsed.path
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if not self._guard(query):
            return
        path = urllib.parse.unquote(raw_path)
        try:
            if path == "/" or path == "":
                return self.route_home()
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if path == "/healthz":
                return self._send(200, b"ok", "text/plain; charset=utf-8")
            if path == "/static/pygments.css":
                return self._send(
                    200,
                    PYGMENTS_CSS.encode("utf-8"),
                    "text/css; charset=utf-8",
                    {"Cache-Control": "public, max-age=600"},
                )
            if path == "/api/browse":
                return self.route_browse(query)
            if path == "/api/meta":
                return self.route_meta(query)
            if path.startswith("/static/"):
                return self.route_static(path[len("/static/"):])
            if path.startswith("/rawpdf/"):
                return self.route_rawpdf(path[len("/rawpdf/"):])
            if path.startswith("/asset/"):
                return self.route_asset(path[len("/asset/"):])
            if path.startswith("/d/"):
                return self.route_doc(path[len("/d/"):], query)
            if path == "/search":
                return self.route_search((query.get("q") or [""])[0])
            return self._not_found()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            if CFG["verbose"]:
                import traceback

                traceback.print_exc()
            return self._html(500, f"<pre>服务出错：{e(exc)}</pre>")

    do_HEAD = do_GET

    def do_POST(self):
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if not self._guard(query):
            return
        route = urllib.parse.unquote(parsed.path)
        try:
            if route == "/api/link":
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                if length <= 0 or length > 65536:
                    return self._json(400, {"ok": False, "error": "请求体不合法"})
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                except Exception:
                    return self._json(400, {"ok": False, "error": "JSON 解析失败"})
                return self.route_link(payload if isinstance(payload, dict) else {})
            return self._not_found()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def route_home(self):
        docs = scan_docs()
        if not docs:
            body = (
                '<article class="doc"><div class="doc-body">'
                "<h1>还没有文档</h1>"
                f"<p>把 Markdown 文件放进 <code>{e(str(CFG['root']))}</code>，刷新本页即可。</p>"
                "<p>子目录会变成左侧的分组。</p></div></article>"
            )
            return self._html(200, page("mdview", sidebar_html(None, docs), body, "", crumbs_for(None)))
        first = docs[0]["rel"]
        bootstrap = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>mdview</title><script>
(function(){{
  var last=null;try{{last=localStorage.getItem("mdview:last");}}catch(e){{}}
  var target=last?("/d/"+encodeURIComponent(last).replace(/%2F/g,"/")):"/d/{q(first)}";
  location.replace(target);
}})();
</script></head><body></body></html>"""
        self._html(200, bootstrap)

    def route_browse(self, query: dict):
        """给“添加目录”弹窗用：列出某个目录下的子目录（只读浏览）。"""
        raw = (query.get("path") or [""])[0]
        try:
            base = Path(os.path.expanduser(raw) if raw else Path.home()).resolve()
        except OSError:
            base = Path.home()
        if not base.is_dir():
            base = Path.home()
        dirs = []
        try:
            for ent in sorted(os.scandir(base), key=lambda e: e.name.casefold()):
                if ent.name.startswith("."):
                    continue
                try:
                    if not ent.is_dir(follow_symlinks=True):
                        continue
                except OSError:
                    continue
                dirs.append({"name": ent.name, "path": str(Path(ent.path))})
                if len(dirs) >= 800:
                    break
        except OSError:
            pass
        self._json(200, {
            "ok": True,
            "path": str(base),
            "parent": str(base.parent) if base != Path(base.anchor) else None,
            "home": str(Path.home()),
            "docs": str(CFG["root"]),
            "dirs": dirs,
        })

    def route_link(self, payload: dict):
        """在 docs 下创建指向某个目录的软链接。"""
        target_raw = str(payload.get("target") or "").strip()
        name_raw = str(payload.get("name") or "").strip()
        if not target_raw:
            return self._json(400, {"ok": False, "error": "缺少目标目录"})

        try:
            target = Path(os.path.expanduser(target_raw)).resolve()
        except OSError:
            return self._json(400, {"ok": False, "error": "路径无法解析"})
        if not target.is_dir():
            return self._json(400, {"ok": False, "error": "目标不是目录或不存在"})

        root = CFG["root"].resolve()
        # 拒绝自身/上级/下级：否则会变成无限递归的目录树
        if target == root or root in target.parents or target in root.parents:
            return self._json(400, {"ok": False, "error": "不能链接 docs 自身、它的上级或内部目录"})
        name = name_raw or target.name
        if (
            not name
            or name in (".", "..")
            or "/" in name
            or "\\" in name
            or name.startswith(".")
        ):
            return self._json(400, {"ok": False, "error": "链接名不合法"})

        link = root / name
        if link.exists() or link.is_symlink():
            return self._json(409, {"ok": False, "error": f"docs 下已有同名条目：{name}"})
        if target == root:
            return self._json(400, {"ok": False, "error": "不能链接 docs 自己"})

        try:
            os.symlink(target, link)
        except OSError as exc:
            return self._json(500, {"ok": False, "error": f"创建失败：{exc.strerror or exc}"})

        # 让扫描缓存立刻失效
        with _scan_lock:
            _scan_state["stamp"] = None
            _scan_state["checked"] = 0.0
        return self._json(200, {"ok": True, "name": name, "target": str(target)})

    def route_meta(self, query: dict):
        """给前端轮询用：只返回 mtime/size，不渲染正文。"""
        rel = (query.get("p") or [""])[0]
        target = resolve_inside(rel)
        if target is None or target.suffix.lower() not in (".md", ".markdown") or not target.is_file():
            return self._send(404, b'{"ok":false}', "application/json; charset=utf-8")
        try:
            st = target.stat()
        except OSError:
            return self._send(404, b'{"ok":false}', "application/json; charset=utf-8")
        payload = {"ok": True, "path": rel, "mtime": st.st_mtime, "size": st.st_size}
        return self._send(
            200,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            {"Cache-Control": "no-store"},
        )

    def route_static(self, name: str):
        name = posixpath.normpath(urllib.parse.unquote(name)).lstrip("/")
        if not name or name.startswith("..") or ".." in name.split("/"):
            return self._not_found()
        try:
            target = (STATIC_DIR / name).resolve()
        except OSError:
            return self._not_found()
        if not target.is_relative_to(STATIC_DIR.resolve()) or not target.is_file():
            return self._not_found()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        # 字体这类大文件可以长缓存；自己的 css/js 每次校验，改完立刻生效
        cache = "public, max-age=604800" if name.startswith("katex/") else "no-cache"
        self._send(200, target.read_bytes(), ctype, {"Cache-Control": cache})

    def route_rawpdf(self, rest: str):
        """原始 PDF：交给浏览器自带的阅读器（平板上的体验最好），支持 Range。"""
        target = resolve_inside(urllib.parse.unquote(rest))
        if target is None or target.suffix.lower() != ".pdf" or not target.is_file():
            return self._not_found("PDF 不存在")
        try:
            size = target.stat().st_size
        except OSError:
            return self._not_found("PDF 不存在")

        start, end, status = 0, max(0, size - 1), 200
        rng = (self.headers.get("Range") or "").strip()
        if rng.startswith("bytes="):
            spec = rng[6:].split(",")[0].strip()
            first, _, last = spec.partition("-")
            try:
                if first:
                    start = int(first)
                    end = int(last) if last else size - 1
                    status = 206
                elif last:
                    start = max(0, size - int(last))
                    status = 206
            except ValueError:
                start, end, status = 0, max(0, size - 1), 200
        start = max(0, min(start, size - 1)) if size else 0
        end = max(start, min(end, size - 1)) if size else 0
        length = max(0, end - start + 1)

        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(length))
            self.send_header(
                "Content-Disposition",
                f"inline; filename*=UTF-8''{urllib.parse.quote(target.name)}",
            )
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            remaining = length
            with target.open("rb") as fh:
                fh.seek(start)
                while remaining > 0:
                    chunk = fh.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def route_asset(self, rest: str):
        target = resolve_inside(urllib.parse.unquote(rest))
        if target is None:
            return self._not_found("路径不合法")
        if not target.is_file():
            return self._not_found("资源不存在")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        return self._send(
            200,
            target.read_bytes(),
            ctype,
            {"Cache-Control": "public, max-age=60"},
        )

    def _neighbors(self, docs: list[dict], rel: str):
        rels = [d["rel"] for d in docs]
        try:
            idx = rels.index(rel)
        except ValueError:
            return None, None
        prev = docs[idx - 1] if idx > 0 else None
        nxt = docs[idx + 1] if idx + 1 < len(docs) else None
        return prev, nxt

    def route_doc(self, rest: str, query: dict):
        rel = posixpath.normpath(urllib.parse.unquote(rest)).lstrip("/")
        doc = get_doc(rel)
        if doc is None:
            return self._not_found("文档不存在，或不在允许的目录内")
        if (query.get("raw") or [""])[0] == "1":
            return self._send(
                200,
                doc["text"].encode("utf-8"),
                "text/plain; charset=utf-8",
                {"Cache-Control": "no-store"},
            )
        all_docs = scan_docs()
        docs = [d for d in all_docs if d.get("kind") == "md"]
        toc = doc["toc"]
        toc_html = ""
        if len(toc) >= 2:
            items = "".join(
                f'<li class="h{h}"><a href="#{hid}">{e(text)}</a></li>'
                for h, text, hid in toc
                if text
            )
            toc_html = (
                f'<details class="toc"><summary>目录 · {len(toc)} 节</summary>'
                f"<ol>{items}</ol></details>"
            )

        prev, nxt = self._neighbors(docs, rel)
        pager = []
        if prev:
            pager.append(
                f'<a class="pager-prev" href="/d/{q(prev["rel"])}">'
                f'<span class="k">← 上一篇</span><span class="v">{e(prev["label"])}</span></a>'
            )
        else:
            pager.append("<span></span>")
        if nxt:
            pager.append(
                f'<a class="pager-next" href="/d/{q(nxt["rel"])}">'
                f'<span class="k">下一篇 →</span><span class="v">{e(nxt["label"])}</span></a>'
            )
        pager_html = f'<nav class="pager">{"".join(pager)}</nav>' if len(docs) > 1 else ""

        sig = hashlib.sha1(doc["html"].encode("utf-8")).hexdigest()[:16]
        body = (
            f'<article class="doc" data-title="{e(doc["title"])}" '
            f'data-mtime="{doc["mtime"]:.6f}" data-sig="{sig}">'
            f'{toc_html}'
            f'<div class="doc-body">{doc["html"]}</div>'
            f'<div class="doc-meta">更新于 {human_time(doc["mtime"])} · {human_size(doc["size"])} · '
            f'<a href="/d/{q(rel)}?raw=1" class="raw-link" target="_blank" rel="noopener">查看原文</a></div>'
            f"{pager_html}"
            "</article>"
        )
        crumbs = crumbs_for(rel, doc["label"])

        # 站内切换（全屏时点另一篇不刷新页面，全屏状态才保得住）
        if (query.get("frag") or [""])[0] == "1":
            payload = {
                "path": rel,
                "title": doc["title"],
                "html": body,
                "crumbs": crumbs,
                "mtime": doc["mtime"],
                "sig": sig,
            }
            return self._send(
                200,
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
                {"Cache-Control": "no-store"},
            )

        self._html(
            200,
            page(
                f'{doc["title"]} · mdview',
                sidebar_html(rel, all_docs),
                body,
                rel,
                crumbs,
            ),
        )

    def route_search(self, query: str):
        query = query.strip()
        docs = scan_docs()
        items = []
        if query:
            needle = query.casefold()
            for d in docs:
                if d.get("kind") != "md":
                    continue
                doc = get_doc(d["rel"])
                if not doc:
                    continue
                hay = doc["text"].casefold()
                pos = hay.find(needle)
                if pos < 0 and needle not in d["name"].casefold() and needle not in d["title"].casefold():
                    continue
                snippet = ""
                if pos >= 0:
                    start = max(0, pos - 70)
                    end = min(len(doc["text"]), pos + len(query) + 70)
                    frag = doc["text"][start:end].replace("\n", " ")
                    low = frag.casefold()
                    out, cursor = [], 0
                    while True:
                        j = low.find(needle, cursor)
                        if j < 0:
                            out.append(e(frag[cursor:]))
                            break
                        out.append(e(frag[cursor:j]))
                        out.append(f"<mark>{e(frag[j:j + len(query)])}</mark>")
                        cursor = j + len(query)
                    snippet = ("…" if start > 0 else "") + "".join(out) + ("…" if end < len(doc["text"]) else "")
                items.append(
                    f'<a class="result" href="/d/{q(d["rel"])}">'
                    f'<div class="r-title">{e(d["title"])}</div>'
                    f'<div class="r-path">{e(d["rel"])}</div>'
                    + (f'<div class="r-snippet">{snippet}</div>' if snippet else "")
                    + "</a>"
                )
        if query and not items:
            results = '<p class="empty">没有匹配的文档。</p>'
        elif not query:
            results = '<p class="empty">输入关键词，在全部文档正文里搜索。</p>'
        else:
            results = f'<p class="hint">{len(items)} 条结果</p>' + "".join(items)

        body = (
            '<article class="doc search-page"><div class="doc-body">'
            f"<h1>全文搜索</h1>"
            f'<form class="search-form" action="/search" method="get">'
            f'<input type="search" name="q" value="{e(query)}" placeholder="关键词" autofocus>'
            f"<button type=\"submit\">搜索</button></form>"
            f"{results}</div></article>"
        )
        self._html(
            200,
            page("搜索 · mdview", sidebar_html(None, docs), body, "", '<span class="crumb-current">搜索</span>'),
        )


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------


def detect_ips() -> list[str]:
    ips: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.3)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips


def print_banner(bind: str, port: int, docs: list[dict], created_token: bool):
    line = "─" * 62
    mode = CFG["auth_mode"]
    token = CFG["auth_token"]
    print(f"\n\033[1mmdview {VERSION}\033[0m  局域网 Markdown 阅读服务")
    print(line)
    print(f"  文档根目录  {CFG['root']}")
    print(f"  文档数量    {len(docs)} 篇")
    if mode == "none":
        print("  访问令牌    \033[33m已关闭（局域网免密）\033[0m")
    elif mode == "basic":
        print(f"  访问口令    {CFG['auth_user']} / \033[1m{token}\033[0m  (浏览器会弹账密框)")
    else:
        state = "新生成" if created_token else "读取自"
        print(f"  访问令牌    \033[1m{token}\033[0m  ({state} {AUTH_FILE})")
        print("              链接里带一次令牌，浏览器记住后不用再输")
    print(line)

    ips = detect_ips()
    host = socket.gethostname()
    lan = [ip for ip in ips if not ip.startswith("127.")]
    if bind not in ("0.0.0.0", "", "::"):
        lan = []

    def url(ip: str) -> str:
        base = f"http://{ip}:{port}/"
        return f"{base}?k={token}" if mode == "token" else base

    print("  这台电脑：")
    print(f"    {url('127.0.0.1')}")
    if lan:
        print("  平板 / 手机（同一 Wi-Fi，复制整条）：")
        for ip in lan:
            print(f"    \033[1;36m{url(ip)}\033[0m")
        print(f"    \033[1;36m{url(host + '.local')}\033[0m   （iPad 可用 .local 域名）")
        if any(ip.startswith("10.69.") for ip in lan):
            print("  注：10.69.x 是校园网段，学生网常开 AP 隔离，打不开见 README")
    print(line)
    print("  Ctrl+C 停止\n")
    sys.stdout.flush()

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="mdview —— 局域网 Markdown 阅读服务")
    parser.add_argument("--root", default=str(APP_DIR / "docs"), help="对外开放的文档根目录")
    parser.add_argument("--port", type=int, default=8899, help="监听端口，默认 8899")
    parser.add_argument("--bind", default="0.0.0.0", help="监听地址，默认 0.0.0.0")
    parser.add_argument(
        "--no-auth", action="store_true", help="完全关闭验证（局域网任何人可看）"
    )
    parser.add_argument(
        "--basic", action="store_true", help="改用 HTTP Basic 账密弹窗（默认是 URL 令牌）"
    )
    parser.add_argument("--token", "--password", dest="token", default=None,
                        help="本次启动使用的访问令牌（不写入文件）")
    parser.add_argument("--verbose", action="store_true", help="打印访问日志")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    CFG["root"] = root
    CFG["verbose"] = args.verbose
    CFG["auth_mode"] = "none" if args.no_auth else ("basic" if args.basic else "token")

    created = False
    if CFG["auth_mode"] != "none":
        if args.token:
            CFG["auth_token"] = args.token
        else:
            existed = AUTH_FILE.is_file()
            user, token = load_or_create_auth()
            CFG["auth_user"], CFG["auth_token"] = user, token
            created = not existed

    docs = scan_docs()
    print_banner(args.bind, args.port, docs, created)

    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
