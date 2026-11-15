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
    assert.deepEqual(errors, []);
    console.log("PASS: 锚点、公式、图表、主题/缩放、文档切换、自动刷新和延迟响应隔离；浏览器错误 0。");
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
