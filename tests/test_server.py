"""文档变更、鉴权、资源访问与 PDF 协议的回归测试。"""
import base64
import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import serve
from rendering import render_markdown


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.01})
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "docs"
        self.root.mkdir()
        self.config = serve.CFG.copy()
        self.addCleanup(serve.CFG.update, self.config)
        serve.CFG.update(root=self.root, auth_mode="none", auth_token="test-token")
        serve._scan_state.update(root=None, checked=0, docs=[])
        serve._meta_cache.clear()
        serve._render_cache.clear()

    def request(self, path="/", method="GET", headers=None, body=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            conn.request(method, path, body, {"Connection": "close", **(headers or {})})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def write(self, name, text="# 文档"):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def rescan(self):
        serve._scan_state["checked"] = 0
        return serve.scan_docs()

    def test_scan_detects_rename_and_changes_below_latest_mtime(self):
        a = self.write("a.md", "# Alpha")
        b = self.write("b.md", "# Bravo")
        os.utime(b, (2_000_000_000, 2_000_000_000))
        serve.scan_docs()
        a.rename(self.root / "c.md")
        self.assertEqual([d["rel"] for d in self.rescan()], ["b.md", "c.md"])
        self.write("c.md", "# Changed title")
        self.assertEqual(self.rescan()[1]["title"], "Changed title")

    def test_empty_scan_is_throttled(self):
        with patch.object(serve, "iter_md_files", wraps=serve.iter_md_files) as scan:
            serve.scan_docs()
            serve.scan_docs()
            self.assertEqual(scan.call_count, 1)

    def test_symlink_loop_does_not_duplicate_root(self):
        self.write("a.md")
        (self.root / "loop").symlink_to(self.root, target_is_directory=True)
        external = Path(self.temp.name) / "external"
        external.mkdir()
        (external / "b.md").write_text("# External")
        (self.root / "linked").symlink_to(external, target_is_directory=True)
        self.assertEqual([d["rel"] for d in serve.scan_docs()], ["a.md", "linked/b.md"])
        self.assertEqual(self.request("/d/linked/b.md")[0], 200)

    def test_pdf_scan_does_not_read_contents(self):
        self.write("large.pdf", "%PDF")
        with patch.object(Path, "open", side_effect=AssertionError("PDF must not be read")):
            self.assertEqual(serve.scan_docs()[0]["kind"], "pdf")

    def test_change_with_same_size_and_mtime_invalidates_render(self):
        target = self.write("a.md", "# Alpha")
        original = serve.get_doc("a.md")
        st = target.stat()
        target.write_text("# Bravo")
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
        updated = serve.get_doc("a.md")
        self.assertNotEqual(original["version"], updated["version"])
        self.assertIn("Bravo", updated["html"])

    def test_render_cache_is_bounded_and_removals_are_pruned(self):
        with patch.object(serve, "MAX_RENDER_CACHE", 2):
            for name in ("a.md", "b.md", "c.md"):
                self.write(name)
                serve.get_doc(name)
            self.assertEqual(len(serve._render_cache), 2)
        (self.root / "b.md").unlink()
        self.rescan()
        self.assertNotIn(self.root / "b.md", serve._render_cache)

    def test_missing_document_is_404(self):
        self.assertEqual(self.request("/d/deleted.md")[0], 404)

    def test_paths_are_decoded_once(self):
        self.write("percent%20name.md", "# Percent name")
        self.assertEqual(self.request("/d/percent%2520name.md")[0], 200)
        self.write(".private/secret.md")
        self.write("node_modules/secret.md")
        for path in ("/d/../secret.md", "/d/.private/secret.md", "/d/node_modules/secret.md", "/d/a%00.md"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)

    def test_token_auth_and_cookie(self):
        serve.CFG["auth_mode"] = "token"
        self.assertEqual(self.request("/healthz")[0], 403)
        self.assertEqual(self.request("/healthz?k=" + quote("错误令牌"))[0], 403)
        status, headers, _ = self.request("/healthz?k=test-token")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/healthz")
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.request("/healthz", headers={"Cookie": cookie})[2], b"ok")

    def test_basic_auth_accepts_utf8_credentials(self):
        serve.CFG.update(auth_mode="basic", auth_user="用户", auth_token="访问口令")
        value = base64.b64encode("用户:访问口令".encode()).decode()
        self.assertEqual(self.request("/healthz", headers={"Authorization": "Basic " + value})[0], 200)

    def test_pdf_range_and_head(self):
        self.write("test.pdf", "0123456789")
        cases = [("bytes=2-4", 206, b"234"), ("bytes=7-", 206, b"789"),
                 ("bytes=-3", 206, b"789"), ("bytes=0-999", 206, b"0123456789"),
                 ("bytes=99-", 416, b""), ("bytes=4-2", 416, b""), ("bytes=-0", 416, b"")]
        for value, expected, content in cases:
            with self.subTest(range=value):
                status, headers, body = self.request("/rawpdf/test.pdf", headers={"Range": value})
                self.assertEqual((status, body), (expected, content))
                self.assertEqual(int(headers["Content-Length"]), len(body))
        status, headers, body = self.request("/rawpdf/test.pdf", method="HEAD")
        self.assertEqual((status, headers["Content-Length"], body), (200, "10", b""))

    def test_empty_pdf_has_zero_length(self):
        self.write("empty.pdf", "")
        self.assertEqual(self.request("/rawpdf/empty.pdf")[2], b"")
        self.assertEqual(self.request("/rawpdf/empty.pdf", headers={"Range": "bytes=0-"})[0], 416)

    def test_html_assets_cannot_execute_as_site_pages(self):
        self.write("unsafe.html", "<script>alert(1)</script>")
        status, headers, _ = self.request("/asset/unsafe.html")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Disposition"].startswith("attachment;"))
        self.assertIn("sandbox", headers["Content-Security-Policy"])

    def test_pdf_only_home_is_readable(self):
        self.write("only.pdf", "%PDF")
        status, _, body = self.request()
        self.assertEqual(status, 200)
        self.assertIn(b'var paths=["only.pdf"]', body)
        self.assertIn(b"location.replace", body)

    def test_pdf_document_has_reader_and_active_sidebar(self):
        self.write("reading.pdf", "%PDF")
        status, _, body = self.request("/d/reading.pdf")
        self.assertEqual(status, 200)
        self.assertIn(b'<li class="file pdf active">', body)
        self.assertIn(b'href="/d/reading.pdf" data-path="reading.pdf"', body)
        self.assertIn(b'<iframe class="pdf-frame" src="/rawpdf/reading.pdf#view=FitH&amp;navpanes=0"', body)
        status, _, body = self.request("/d/reading.pdf?frag=1")
        self.assertEqual(status, 200)
        fragment = json.loads(body)
        self.assertEqual(fragment["path"], "reading.pdf")
        self.assertIn('class="pdf-frame"', fragment["html"])
        self.assertNotIn('<aside', fragment["html"])
        self.assertEqual(self.request("/d/missing.pdf")[0], 404)

    def test_search_does_not_render_documents(self):
        self.write("a.md", "# Searching\n\nİSTANBUL and <needle>\n\n```python\nprint(42)\n```")
        with patch.object(serve, "render_markdown", side_effect=AssertionError("Search must not render")):
            status, _, body = self.request("/search?q=needle")
        self.assertEqual(status, 200)
        self.assertIn(b"<mark>needle</mark>", body)
        self.assertIn(b"&lt;", body)

    def test_link_validation_and_creation(self):
        target = Path(self.temp.name) / "external"
        target.mkdir()
        headers = {"Content-Type": "application/json"}
        def link(path, name="linked", extra=None):
            return self.request("/api/link", "POST", {**headers, **(extra or {})}, json.dumps({"target": str(path), "name": name}))
        self.assertEqual(link(target)[0], 200)
        self.assertTrue((self.root / "linked").is_symlink())
        self.assertEqual(link(target)[0], 409)
        self.assertEqual(link(self.root)[0], 400)
        self.assertEqual(link(target, "bad\x00name")[0], 400)
        self.assertEqual(link(target, extra={"Origin": "https://elsewhere.example"})[0], 403)
        self.assertEqual(self.request("/api/link", "POST", body="{}")[0], 415)

    def test_fragment_and_metadata_have_same_version(self):
        self.write("a.md")
        meta = json.loads(self.request("/api/meta?p=a.md")[2])
        fragment = json.loads(self.request("/d/a.md?frag=1")[2])
        self.assertEqual(meta["version"], fragment["version"])


class RenderingTests(unittest.TestCase):
    def test_unsafe_html_is_removed(self):
        cases = ["<script>alert(1)</script>", '<img src=x onerror="alert(1)">',
                 '<a href=javascript:alert(1)>bad</a>', '<a href="jav&#x61;script:alert(1)">bad</a>',
                 '<iframe srcdoc="<script>alert(1)</script>"></iframe>',
                 '<a href="java&#10;script:alert(1)">bad</a>']
        for markup in cases:
            with self.subTest(markup=markup):
                rendered = render_markdown(markup, "")["html"]
                self.assertNotIn("alert(1)", rendered)
                self.assertNotIn("<script", rendered)

    def test_links_keep_query_and_fragment(self):
        body = render_markdown("[next](other.md?raw=1#s2)\n\n<img src='a%20b.png'>", "folder")["html"]
        self.assertIn('/d/folder/other.md?raw=1#s2', body)
        self.assertIn('/asset/folder/a%20b.png', body)
        self.assertIn('/d/folder/paper.pdf', render_markdown('[PDF](paper.pdf)', 'folder')["html"])

    def test_code_mermaid_tasks_and_table_alignment(self):
        body = render_markdown("```python\nprint(1)\n```\n\n```mermaid\ngraph LR\n A-->B\n```\n\n- [x] done\n\n| a |\n|:--|\n| 1 |", "")["html"]
        for expected in ('class="copy-btn"', 'class="mermaid"', 'class="task done"', 'class="table-wrap"', 'text-align:left'):
            self.assertIn(expected, body)


if __name__ == "__main__":
    unittest.main()
