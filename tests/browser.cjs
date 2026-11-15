/* Chrome 回归：锚点、公式/图表、自动刷新及切换文档时的异步竞态。 */
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const net = require("node:net");
const { spawn } = require("node:child_process");
let chromium;
try {
  ({ chromium } = require("playwright"));
} catch {
  ({ chromium } = require(path.resolve(path.dirname(process.execPath), "../lib/node_modules/playwright")));
}

const repo = path.resolve(__dirname, "..");
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function waitFor(check, timeout = 10000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (await check()) return;
    await delay(50);
  }
  throw new Error("等待条件超时");
}

(async () => {
  const temp = await fs.mkdtemp(path.join(os.tmpdir(), "mdview-browser-"));
  let server, browser, failure;
  try {
    const a = "# 文档 A\n\n## 第二节\n\n[A link](B.md#s2)\n\n公式：$x_i + y_i = z_i$\n\n```mermaid\ngraph LR\n A[文档] --> B[阅读]\n```\n\n```python\nprint('hello')\n```\n\n初始正文\n";
    await fs.writeFile(path.join(temp, "A.md"), a);
    await fs.writeFile(path.join(temp, "B.md"), "# 文档 B\n\n## 第二节\n\n保持正文 B\n");
    await fs.writeFile(path.join(temp, "参考.pdf"), "%PDF");
    const reservation = net.createServer();
    await new Promise(resolve => reservation.listen(0, "127.0.0.1", resolve));
    const port = reservation.address().port;
    await new Promise(resolve => reservation.close(resolve));
    const base = `http://127.0.0.1:${port}`;
    const python = process.env.MDVIEW_PYTHON || path.join(repo, ".venv/bin/python");
    server = spawn(python, [path.join(repo, "serve.py"), "--root", temp, "--bind", "127.0.0.1", "--port", String(port), "--no-auth"], { stdio: ["ignore", "ignore", "pipe"] });
    server.stderr.on("data", chunk => { failure = String(chunk); });
    await waitFor(async () => {
      if (server.exitCode !== null) throw new Error(failure || "服务退出");
      try { return (await fetch(base + "/healthz")).ok; } catch { return false; }
    });
    browser = await chromium.launch({ channel: "chrome", headless: true });
    const pdfPage = await browser.newPage();
    await pdfPage.setContent('<html><body style="padding:48px;font-family:sans-serif"><h1>PDF reading in mdview</h1><p>The directory stays visible while reading this document.</p><p>Use the PDF toolbar to change pages or zoom.</p></body></html>');
    await pdfPage.pdf({ path: path.join(temp, "参考.pdf"), format: "A4" });
    await pdfPage.close();
    const page = await browser.newPage({ viewport: { width: 1024, height: 768 } });
    const errors = [];
    page.on("pageerror", error => errors.push(String(error)));
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    page.on("response", response => { if (response.status() >= 400) errors.push(`${response.status()} ${response.url()}`); });

    await page.goto(base + "/d/A.md#s2");
    await page.waitForSelector(".katex");
    await page.waitForSelector(".mermaid svg");
    await page.waitForSelector(".mm-tools");
    await page.locator("#zoom-in").click();
    assert.equal(await page.locator("#zoom-val").textContent(), "110%");
    await page.locator("#theme").click();
    await page.waitForSelector(".mermaid svg");
    await page.screenshot({ path: "/tmp/mdview-review-landscape.png" });
    await page.setViewportSize({ width: 820, height: 1180 });
    await page.screenshot({ path: "/tmp/mdview-review-portrait.png" });
    await page.locator('.doc-body a[href="/d/B.md#s2"]').click();
    await page.waitForFunction(() => document.body.dataset.path === "B.md");
    assert.equal(new URL(page.url()).hash, "#s2");
    await page.goBack();
    await page.waitForFunction(() => document.body.dataset.path === "A.md");
    assert.equal(new URL(page.url()).hash, "#s2");

    // 保持 mtime，验证内容版本仍能触发自动刷新。
    const st = await fs.stat(path.join(temp, "A.md"));
    await fs.writeFile(path.join(temp, "A.md"), a.replace("初始正文", "更新正文"));
    await fs.utimes(path.join(temp, "A.md"), st.atime, st.mtime);
    await page.waitForFunction(() => document.querySelector(".doc-body").textContent.includes("更新正文"), null, { timeout: 12000 });

    // 暂存 A 的刷新响应，切换 B 后再返回，A 不应覆盖 B。
    let release, received;
    const blocked = new Promise(resolve => { received = resolve; });
    const gate = new Promise(resolve => { release = resolve; });
    let blockedOnce = false;
    await page.route("**/d/A.md?frag=1", async route => {
      if (blockedOnce) return route.continue();
      blockedOnce = true;
      const response = await route.fetch();
      received();
      await gate;
      await route.fulfill({ response });
    });
    await fs.writeFile(path.join(temp, "A.md"), a.replace("初始正文", "延迟正文 A"));
    await Promise.race([blocked, delay(12000).then(() => { throw new Error("自动刷新未发出"); })]);
    await page.locator('#tree a[data-path="B.md"]').click();
    await page.waitForFunction(() => document.body.dataset.path === "B.md");
    release();
    await delay(300);
    assert.match(await page.locator(".doc-body").textContent(), /保持正文 B/);
    assert.doesNotMatch(await page.locator(".doc-body").textContent(), /延迟正文 A/);

    // PDF 与 Markdown 共用页面外壳，阅读器在右侧加载，返回后保留目录。
    await page.evaluate(() => { window.pdfShellMarker = true; });
    const pdfResponse = page.waitForResponse(response => response.url().includes("/rawpdf/") && response.status() === 200);
    await page.locator('#tree a[data-path="参考.pdf"]').click();
    await page.waitForSelector(".pdf-frame");
    assert.equal((await pdfResponse).headers()["content-type"], "application/pdf");
    assert.equal(await page.evaluate(() => window.pdfShellMarker), true);
    assert.equal(await page.locator('#tree li.pdf.active a').getAttribute('data-path'), "参考.pdf");
    assert.equal(await page.locator('.topbar .zoom').isVisible(), false);
    assert.equal(page.context().pages().length, 1);
    await delay(1500); // 等待浏览器内置 PDF 阅读器绘制页面。
    await page.screenshot({ path: "/tmp/mdview-pdf-portrait.png" });
    await page.setViewportSize({ width: 1024, height: 768 });
    await delay(400);
    await page.screenshot({ path: "/tmp/mdview-pdf-landscape.png" });
    const frame = await page.locator('.pdf-frame').boundingBox();
    assert.ok(frame.x >= 288 && frame.width > 700 && frame.height > 600);
    await delay(4300);
    await page.goBack();
    await page.waitForFunction(() => document.body.dataset.path === "B.md");
    assert.equal(await page.locator('.topbar .zoom').isVisible(), true);
    assert.equal(await page.evaluate(() => window.pdfShellMarker), true);
    await page.goForward();
    await page.waitForSelector('.pdf-frame');
    await page.reload();
    await page.waitForSelector('.pdf-frame');
    await page.goto(base + '/');
    await page.waitForFunction(() => document.body.dataset.path === "参考.pdf");
    await page.waitForSelector('.pdf-frame');
    assert.deepEqual(errors, []);
    console.log("PASS: PDF 内嵌阅读/切换/最近阅读，以及 Markdown、自动刷新回归；浏览器错误 0。");
    console.log("截图：/tmp/mdview-review-landscape.png /tmp/mdview-review-portrait.png");
  } finally {
    if (browser) await browser.close();
    if (server && server.exitCode === null) {
      const exited = new Promise(resolve => server.once("exit", resolve));
      server.kill("SIGTERM");
      await exited;
    }
    await fs.rm(temp, { recursive: true, force: true });
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
