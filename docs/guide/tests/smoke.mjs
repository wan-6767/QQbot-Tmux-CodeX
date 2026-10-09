import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, mkdir, stat } from 'node:fs/promises';
import { dirname, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { chromium } from 'playwright';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const results = resolve(root, 'test-results');
await mkdir(results, { recursive: true });
const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.png': 'image/png' };
const server = createServer(async (req, res) => {
  try {
    const path = resolve(root, '.' + decodeURIComponent(new URL(req.url, 'http://localhost').pathname));
    if (!path.startsWith(root + sep)) { res.writeHead(403).end(); return; }
    const file = (await stat(path)).isDirectory() ? resolve(path, 'index.html') : path;
    res.setHeader('Content-Type', types[file.slice(file.lastIndexOf('.'))] || 'application/octet-stream');
    res.end(await readFile(file));
  } catch { res.writeHead(404).end(); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
let browser;
let assertions = 0;
function check(value, message) { assertions++; assert.ok(value, message); }
async function navigate(page, view) {
  await page.locator(`[data-view=${view}]`).click();
  await page.locator(`[data-view=${view}][aria-current=page]`).waitFor();
}
async function fits(page, label) {
  const size = await page.evaluate(() => ({ content: document.documentElement.scrollWidth, viewport: innerWidth }));
  check(size.content <= size.viewport, `${label}: page overflow ${JSON.stringify(size)}`);
  const clipped = await page.locator('button, h1, h2, .field, .command-row, .code-tool').evaluateAll(nodes => nodes.filter(n => {
    const r = n.getBoundingClientRect(); return r.width > 0 && (r.left < -1 || r.right > innerWidth + 1 || n.scrollWidth > n.clientWidth + 2);
  }).map(n => n.textContent.slice(0, 80)));
  check(!clipped.length, `${label}: clipped controls ${JSON.stringify(clipped)}`);
}
try {
  browser = await chromium.launch({ headless: true });
  if (process.env.UPDATE_ASSETS === '1') {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
    await page.goto(origin + '/index.html');
    await page.locator('.brand-mark svg').waitFor();
    await page.locator('.brand-mark').screenshot({ path: resolve(root, 'assets/mark.png') });
    await page.screenshot({ path: resolve(root, 'assets/workbench.png') });
    await page.close();
  }
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, permissions: ['clipboard-read', 'clipboard-write'] });
  const page = await context.newPage();
  const errors = [], remote = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', e => { if (e.type() === 'error') errors.push(e.text()); });
  page.on('request', r => { if (!r.url().startsWith(origin)) remote.push(r.url()); });
  page.on('response', r => { if (r.status() >= 400) errors.push(`HTTP ${r.status()} ${r.url()}`); });
  await page.goto(origin + '/index.html');
  await page.locator('#instance-name').waitFor();
  check(await page.locator('h1').textContent() === 'QQbot-Tmux', 'Product name visible');
  const version = JSON.parse(await readFile(resolve(root, 'package.json'), 'utf8')).version;
  check((await page.locator('body').textContent()).includes('v' + version), 'Visible guide version matches package');
  check(await page.locator('[type=password]').count() === 0, 'No credential intake');
  await fits(page, 'desktop setup');
  await page.screenshot({ path: resolve(results, 'desktop-setup.png') });
  await page.locator('[data-copy-code=initialize]').click();
  check(await page.evaluate(() => navigator.clipboard.readText()) === await page.locator('[data-code=initialize]').textContent(), 'Copies actual generated command');
  await page.locator('#instance-name').fill('second-bot');
  await page.locator('#bridge-port').fill('18222');
  check((await page.locator('[data-code=initialize]').textContent()).includes('init second-bot --port 18222'), 'Custom instance parameters');
  await page.locator('#instance-name').fill('x; touch /tmp/injected');
  check(await page.locator('#instance-name').getAttribute('aria-invalid') === 'true', 'Shell characters rejected');
  check(await page.locator('[data-copy-code=initialize]').isDisabled(), 'Invalid command cannot copy');
  check(!(await page.locator('[data-code=initialize]').textContent()).includes('touch'), 'Invalid value not interpolated');
  await page.locator('#instance-name').fill('second-bot');
  await page.locator('#bridge-port').fill('22');
  check(await page.locator('#bridge-port').getAttribute('aria-invalid') === 'true', 'Privileged port rejected');
  await page.locator('#bridge-port').fill('65536');
  check(await page.locator('[data-copy-code=initialize]').isDisabled(), 'Out-of-range port rejected');
  await page.locator('#bridge-port').fill('18222');
  await page.locator('[data-check="0"]').check();
  await page.locator('[data-next]').click();
  check((await page.locator('[data-code=credentials]').textContent()).includes('instances/second-bot/bot.env'), 'Credential file command');
  check((await page.locator('#step-body').textContent()).includes('沙箱'), 'QQ platform boundary visible');
  await page.locator('[data-next]').click();
  const launch = await page.locator('[data-code=launch]').textContent();
  check(launch.includes('qq-tmux-bridge-second-bot.service') && launch.includes('-p qq-tmux-second-bot'), 'Consistent deployment names');
  await page.reload();
  check(await page.locator('#instance-name').inputValue() === 'second-bot', 'Configuration persists');
  check(await page.locator('[data-check="0"]').isChecked(), 'Checklist persists');
  check(await page.locator('[data-step="2"]').getAttribute('aria-selected') === 'true', 'Step persists');
  await page.locator('[data-next]').click();
  check((await page.locator('[data-code=pairing]').textContent()).includes('pairing second-bot'), 'Real server pairing command');
  check((await page.locator('#step-body').textContent()).includes('/group bind'), 'Optional group binding covered');
  await page.locator('#reset-progress').click();
  check(await page.locator('#instance-name').inputValue() === 'default', 'Reset configuration');
  check(!(await page.locator('[data-check="0"]').isChecked()), 'Reset checklist');
  for (const view of ['overview', 'commands', 'operations', 'troubleshooting', 'security']) {
    await navigate(page, view);
    await fits(page, `desktop ${view}`);
    check(await page.locator(`[data-view=${view}]`).getAttribute('aria-current') === 'page', `${view} navigation state`);
    if (view === 'overview') {
      await page.locator('.preview img').scrollIntoViewIfNeeded();
      check(await page.locator('.preview img').evaluate(img => img.complete && img.naturalWidth > 0), 'Actual product screenshot loaded');
    }
  }
  await navigate(page, 'commands');
  await page.locator('[data-category=屏幕]').click();
  check(await page.locator('.command-row').count() === 1, 'Category filter');
  await page.locator('#command-search').fill('tail 100');
  check(await page.locator('.command-row').count() === 1, 'Search combined with category');
  await page.locator('.command-row button').click();
  check(await page.evaluate(() => navigator.clipboard.readText()) === '/tmux sel 001 tail 100', 'Command copy');
  await page.locator('#command-search').fill('nonexistent-command');
  check(await page.locator('.empty-state').count() === 1, 'Empty search state');
  await navigate(page, 'troubleshooting');
  await page.locator('summary').first().click();
  check(await page.locator('details').first().getAttribute('open') !== null, 'Diagnostic accordion expands');
  for (const viewport of [{ width: 390, height: 844 }, { width: 320, height: 740 }]) {
    await page.setViewportSize(viewport);
    for (const view of ['setup', 'overview', 'commands', 'operations', 'troubleshooting', 'security']) {
      await navigate(page, view);
      await fits(page, `${viewport.width} ${view}`);
    }
    await navigate(page, 'setup');
    for (let step = 0; step < 4; step++) {
      await page.locator(`[data-step="${step}"]`).click();
      await fits(page, `${viewport.width} setup step ${step}`);
    }
    await page.locator('[data-step="0"]').click();
    await page.locator('#toast').waitFor({ state: 'hidden' });
    await page.screenshot({ path: resolve(results, `mobile-${viewport.width}.png`), fullPage: true });
  }
  check(errors.length === 0, `Browser errors: ${errors.join('\n')}`);
  check(remote.length === 0, `No telemetry or remote assets: ${remote.join('\n')}`);
  await context.close();
  const offline = await browser.newPage();
  const offlineErrors = [];
  offline.on('pageerror', e => offlineErrors.push(e.message));
  await offline.goto(pathToFileURL(resolve(root, 'index.html')).href);
  check(await offline.locator('#instance-name').count() === 1, 'Direct HTML opening works');
  await offline.locator('[data-view=commands]').click();
  await offline.locator('.command-row').first().waitFor();
  check(await offline.locator('.command-row').count() > 15, 'Offline command reference works');
  check(!offlineErrors.length, `Offline errors: ${offlineErrors.join('\n')}`);
  await offline.goto(pathToFileURL(resolve(root, 'index.html')).href + '#main');
  await offline.reload();
  check(await offline.locator('#instance-name').count() === 1, 'Direct skip-link URL still renders');
  await offline.close();
  const restricted = await browser.newContext();
  await restricted.addInitScript(() => {
    Object.defineProperty(window, 'localStorage', { get() { throw new Error('storage blocked'); } });
    Object.defineProperty(navigator, 'clipboard', { value: { writeText() { return Promise.reject(new Error('clipboard denied')); } } });
  });
  const limited = await restricted.newPage();
  await limited.goto(origin + '/index.html');
  await limited.locator('#instance-name').fill('no-storage');
  check((await limited.locator('[data-code=initialize]').textContent()).includes('init no-storage'), 'Works without storage');
  await limited.locator('[data-copy-code=initialize]').click();
  await limited.waitForFunction(() => document.querySelector('#toast').textContent.includes('未成功'));
  check((await limited.locator('#toast').textContent()).includes('未成功'), 'Clipboard rejection is not reported as success');
  await restricted.close();
  console.log(`PASS: ${assertions} assertions; real Chromium desktop/mobile/offline, controls, clipboard, assets, privacy.`);
} finally {
  await browser?.close();
  await new Promise(resolve => server.close(resolve));
}
