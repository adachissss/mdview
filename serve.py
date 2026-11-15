#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mdview —— 局域网 Markdown 与 PDF 阅读服务

设计要点
  * 从一个文档根目录读取 Markdown、PDF 及资源，支持用户建立的软链接。
  * 左侧可折叠目录树 + 右侧渲染，服务端渲染，平板友好。
  * HTTP Basic 口令 + 一次性 ?k= 换 cookie，局域网内有门槛。
  * 文档只读；添加目录接口仅创建用户指定的软链接。

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
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from rendering import PYGMENTS_CSS, e, q, render_markdown

VERSION = "1.4"
APP_DIR = Path(__file__).resolve().parent
AUTH_FILE = APP_DIR / "auth"          # 口令文件也放在项目里，整个项目自包含
COOKIE_NAME = "mdview_sess"
STATIC_DIR = APP_DIR / "static"


def asset_version() -> str:
    """静态资源指纹：前端文件任一改动都会变，避免平板上拿到旧脚本。"""
    parts = [VERSION]
    for f in (STATIC_DIR / "app.css", STATIC_DIR / "app.js", APP_DIR / "serve.py", APP_DIR / "rendering.py"):
        try:
            parts.append(str(f.stat().st_mtime_ns))
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
_scan_state = {"docs": [], "root": None, "checked": 0.0}
_meta_lock = threading.Lock()
_meta_cache: dict[Path, tuple] = {}
_render_cache: OrderedDict[Path, tuple] = OrderedDict()
MAX_RENDER_CACHE = 64

SKIP_DIRS = {"node_modules", ".git", ".svn", "__pycache__", ".venv", "venv", ".idea", ".vscode"}


def file_version(st: os.stat_result) -> str:
    return f"{st.st_dev}:{st.st_ino}:{st.st_mtime_ns}:{st.st_ctime_ns}:{st.st_size}"


def iter_md_files(root: Path):
    """遍历根目录下的 .md / .pdf，跟随软链接（ln -s 进来的外部目录也能用）。

    用 (dev, inode) 去重，避免软链接互相指向造成死循环。
    """
    seen: set[tuple[int, int]] = set()
    stack: list[Path] = [root]
    while stack:
        cur = stack.pop()
        try:
            st = cur.stat()
            key = (st.st_dev, st.st_ino)
            if key in seen:
                continue
            seen.add(key)
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
                stack.append(Path(ent.path))
            elif stat.S_ISREG(st.st_mode):
                p = Path(ent.path)
                if p.suffix.lower() in (".md", ".markdown", ".pdf"):
                    yield p, st


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def extract_title(path: Path, rel: str) -> str:
    """取标题：优先 H1；没有 H1 就退到第一个任意级标题；再没有就用文件名。"""
    stem = Path(rel).stem
    try:
        with path.open("rb") as fh:
            head = fh.read(8192).decode("utf-8", "replace")
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


def doc_meta(path: Path, rel: str, st: os.stat_result) -> dict:
    key = file_version(st)
    stem = Path(rel).stem
    kind = "pdf" if path.suffix.lower() == ".pdf" else "md"
    title = stem
    if kind == "md":
        with _meta_lock:
            cached = _meta_cache.get(path)
        if cached and cached[0] == key:
            title = cached[1]
        else:
            title = extract_title(path, rel)
            with _meta_lock:
                _meta_cache[path] = (key, title)
    return {
        "rel": rel,
        "name": stem,
        "label": stem,          # 侧栏一律显示文件名，标题只在悬停提示里
        "title": title,
        "kind": kind,
        "mtime": st.st_mtime,
        "size": st.st_size,
        "version": key,
    }


def scan_docs() -> list[dict]:
    """每秒至多扫描一次，复用文件元数据并清理已移除文件的缓存。"""
    root = CFG["root"]
    with _scan_lock:
        now = time.monotonic()
        if _scan_state["root"] == root and now - _scan_state["checked"] < 1.0:
            return _scan_state["docs"]
        docs = []
        for path, st in iter_md_files(root):
            rel = path.relative_to(root).as_posix()
            docs.append(doc_meta(path, rel, st))
        docs.sort(key=lambda d: d["rel"].casefold())
        active = {root / d["rel"] for d in docs}
        with _meta_lock:
            for cache in (_meta_cache, _render_cache):
                for path in list(cache):
                    if path not in active:
                        del cache[path]
        _scan_state.update(docs=docs, root=root, checked=time.monotonic())
        return docs


# ---------------------------------------------------------------------------
# 文档读取
# ---------------------------------------------------------------------------


def resolve_inside(rel: str) -> Path | None:
    """把请求路径按字面限制在根目录内。

    不做 realpath：docs 里的软链接允许指向外部目录（用户自己 ln -s 进来的素材），
    但仍然拒绝 .. 越级和隐藏文件，服务本身是只读的。
    """
    if not rel or rel.startswith("/") or "\x00" in rel or "\\" in rel:
        return None
    parts = rel.split("/")
    if any(not p or p.startswith(".") or p in SKIP_DIRS for p in parts):
        return None
    return CFG["root"] / rel


def get_doc(rel: str) -> dict | None:
    target = resolve_inside(rel)
    if target is None or target.suffix.lower() not in (".md", ".markdown"):
        return None
    try:
        st = target.stat()
        if not stat.S_ISREG(st.st_mode):
            return None
        version = file_version(st)
        with _meta_lock:
            cached = _render_cache.get(target)
            if cached and cached[0] == version:
                _render_cache.move_to_end(target)
                return cached[1]
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
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
        "version": version,
    }
    with _meta_lock:
        _render_cache[target] = (version, payload)
        _render_cache.move_to_end(target)
        while len(_render_cache) > MAX_RENDER_CACHE:
            _render_cache.popitem(last=False)
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
        active = " active" if doc["rel"] == current else ""
        kind = " pdf" if doc.get("kind") == "pdf" else ""
        badge = '<span class="ext">PDF</span>' if kind else ""
        out.append(
            f'<li class="file{kind}{active}">'
            f'<a href="/d/{q(doc["rel"])}" data-path="{e(doc["rel"])}" {data} '
            f'title="{e(doc["title"])}">{e(doc["label"])}{badge}</a></li>'
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
        raise ValueError("auth 文件需要包含有效的 用户名:口令")

    user = "md"
    pwd = secrets.token_urlsafe(24)
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(AUTH_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        got = _read_auth(AUTH_FILE)
        if got:
            return got
        raise ValueError("auth 文件需要包含有效的 用户名:口令") from None
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(f"# mdview 访问口令，请勿提交或分享\n{user}:{pwd}\n")
    return user, pwd


def session_token(pwd: str) -> str:
    return hmac.new(pwd.encode(), b"mdview-session-v1", hashlib.sha256).hexdigest()


def same_secret(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


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
        if key and same_secret(key, token):
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
            if name == COOKIE_NAME and same_secret(value, session_token(token)):
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
                if same_secret(user, CFG["auth_user"]) and same_secret(given, token):
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
        self.close_connection = True
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if not self._guard(query):
            return
        route = urllib.parse.unquote(parsed.path)
        try:
            if route == "/api/link":
                if self.headers.get_content_type() != "application/json":
                    self.close_connection = True
                    return self._json(415, {"ok": False, "error": "请使用 application/json"})
                origin = self.headers.get("Origin")
                if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host"):
                    self.close_connection = True
                    return self._json(403, {"ok": False, "error": "不允许跨站创建目录链接"})
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                if length <= 0 or length > 65536:
                    self.close_connection = True
                    return self._json(400, {"ok": False, "error": "请求体不合法"})
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                except Exception:
                    return self._json(400, {"ok": False, "error": "JSON 解析失败"})
                return self.route_link(payload if isinstance(payload, dict) else {})
            return self._not_found()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (OSError, ValueError):
            self.close_connection = True
            self._json(400, {"ok": False, "error": "目录请求不合法或无法访问"})

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
        paths = [d["rel"] for d in docs if d["kind"] == "md"] + [d["rel"] for d in docs if d["kind"] == "pdf"]
        available_json = json.dumps(paths, ensure_ascii=True).replace("<", "\\u003c")
        bootstrap = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>mdview</title><script>
(function(){{
  var last=null;try{{last=localStorage.getItem("mdview:last");}}catch(e){{}}
  var paths={available_json};
  var target="/d/"+encodeURIComponent(paths.indexOf(last)>=0?last:paths[0]).replace(/%2F/g,"/");
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
            or "\x00" in name
            or name in SKIP_DIRS
            or name.startswith(".")
        ):
            return self._json(400, {"ok": False, "error": "链接名不合法"})

        link = root / name
        if link.exists() or link.is_symlink():
            return self._json(409, {"ok": False, "error": f"docs 下已有同名条目：{name}"})
        try:
            os.symlink(target, link)
        except FileExistsError:
            return self._json(409, {"ok": False, "error": f"docs 下已有同名条目：{name}"})
        except OSError as exc:
            return self._json(500, {"ok": False, "error": f"创建失败：{exc.strerror or exc}"})

        # 让扫描缓存立刻失效
        with _scan_lock:
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
        payload = {"ok": True, "path": rel, "mtime": st.st_mtime, "size": st.st_size, "version": file_version(st)}
        return self._send(
            200,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            {"Cache-Control": "no-store"},
        )

    def route_static(self, name: str):
        if not name or "\x00" in name or any(p.startswith(".") for p in name.split("/")):
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
        self._file(target, ctype, {"Cache-Control": cache})

    def _file(self, target: Path, ctype: str, extra: dict | None = None, ranges: bool = False):
        """分块发送文件；HEAD 不读取正文，PDF 支持单段 Range。"""
        try:
            fh = target.open("rb")
        except OSError:
            return self._not_found("资源不存在或无法读取")
        with fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode):
                return self._not_found("资源不是普通文件")
            size = st.st_size
            start, end, status = 0, size - 1, 200
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip()) if ranges else None
            if match and any(match.groups()):
                first, last = match.groups()
                if first:
                    start = int(first)
                    end = min(int(last), size - 1) if last else size - 1
                else:
                    start = max(0, size - int(last))
                if size == 0 or start >= size or end < start:
                    return self._send(416, b"", ctype, {"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"})
                status = 206
            length = max(0, end - start + 1)
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if ranges:
                self.send_header("Accept-Ranges", "bytes")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command == "HEAD":
                return
            fh.seek(start)
            while length:
                chunk = fh.read(min(65536, length))
                if not chunk:
                    self.close_connection = True
                    break
                self.wfile.write(chunk)
                length -= len(chunk)

    def route_rawpdf(self, rest: str):
        target = resolve_inside(rest)
        if target is None or target.suffix.lower() != ".pdf":
            return self._not_found("PDF 不存在")
        return self._file(target, "application/pdf", {
            "Content-Disposition": f"inline; filename*=UTF-8''{urllib.parse.quote(target.name)}",
            "Cache-Control": "private, no-cache",
        }, ranges=True)

    def route_asset(self, rest: str):
        target = resolve_inside(rest)
        if target is None:
            return self._not_found("路径不合法")
        if target.suffix.lower() == ".pdf":
            return self.route_rawpdf(rest)
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        headers = {
            "Cache-Control": "private, no-cache",
            "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'",
        }
        if not ctype.startswith("image/"):
            headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{urllib.parse.quote(target.name)}"
        return self._file(target, ctype, headers)

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
        rel = rest
        if Path(rel).suffix.lower() == ".pdf":
            return self.route_pdf(rel, query)
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
            f'data-version="{doc["version"]}" data-sig="{sig}">'
            f'{toc_html}'
            f'<div class="doc-body">{doc["html"]}</div>'
            f'<div class="doc-meta">更新于 {human_time(doc["mtime"])} · {human_size(doc["size"])} · '
            f'<a href="/d/{q(rel)}?raw=1" class="raw-link" target="_blank" rel="noopener">查看原文</a></div>'
            f"{pager_html}"
            "</article>"
        )
        return self._document_page(doc, body, query, all_docs, sig)

    def route_pdf(self, rel: str, query: dict):
        target = resolve_inside(rel)
        if target is None:
            return self._not_found("PDF 不存在")
        try:
            st = target.stat()
        except OSError:
            return self._not_found("PDF 不存在")
        if not stat.S_ISREG(st.st_mode):
            return self._not_found("PDF 不存在")
        doc = doc_meta(target, rel, st)
        source = f"/rawpdf/{q(rel)}"
        body = (
            '<section class="pdf-view" aria-label="PDF 阅读">'
            f'<iframe class="pdf-frame" src="{source}#view=FitH&amp;navpanes=0" title="{e(target.name)}"></iframe>'
            '</section>'
        )
        return self._document_page(doc, body, query, scan_docs())

    def _document_page(self, doc: dict, body: str, query: dict, docs: list[dict], sig: str = ""):
        rel = doc["rel"]
        crumbs = crumbs_for(rel, doc["label"])

        # 站内切换（全屏时点另一篇不刷新页面，全屏状态才保得住）
        if (query.get("frag") or [""])[0] == "1":
            payload = {
                "path": rel,
                "title": doc["title"],
                "html": body,
                "crumbs": crumbs,
                "mtime": doc["mtime"],
                "version": doc["version"],
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
                sidebar_html(rel, docs),
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
            pattern = re.compile(re.escape(query), re.IGNORECASE)
            for d in docs:
                if d.get("kind") != "md":
                    continue
                target = resolve_inside(d["rel"])
                if target is None:
                    continue
                try:
                    text = target.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                match = pattern.search(text)
                if match is None and not pattern.search(d["name"]) and not pattern.search(d["title"]):
                    continue
                snippet = ""
                if match:
                    start = max(0, match.start() - 70)
                    end = min(len(text), match.end() + 70)
                    frag = text[start:end].replace("\n", " ")
                    out, cursor = [], 0
                    for hit in pattern.finditer(frag):
                        out.append(e(frag[cursor:hit.start()]))
                        out.append(f"<mark>{e(hit.group())}</mark>")
                        cursor = hit.end()
                    out.append(e(frag[cursor:]))
                    snippet = ("…" if start > 0 else "") + "".join(out) + ("…" if end < len(text) else "")
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
