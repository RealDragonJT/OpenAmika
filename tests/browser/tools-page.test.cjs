const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const fs = require('node:fs/promises');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
let browser;
before(async () => { browser = await chromium.launch(); });
after(async () => { await browser?.close(); });

async function open(t, width) {
  const context = await browser.newContext({ viewport: { width, height: 900 } });
  t.after(() => context.close());
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  t.after(() => assert.deepEqual(errors, []));
  const state = {
    ok: true, enabled: true, clerk_role: 'surface', limits: {}, results: [],
    iot_disclaimer: { accepted: true, version: '2026-11-03' },
    config: { enabled: true, mcpServers: { fixture: { enabled: true, url: 'https://example.com/mcp' } } },
    servers: { fixture: { enabled: true, transport: 'http', kind: 'mcp', target: 'https://example.com/mcp',
      connected: false, connection_state: 'auth_required', tools: [] } },
  };
  const calls = [];
  await page.route('**/*', async route => {
    const u = new URL(route.request().url());
    if (u.hostname !== 'localhost') return route.fulfill({ body: '', contentType: 'text/plain' });
    if (u.pathname === '/tools' || u.pathname.startsWith('/ui/')) {
      const file = path.join(root, u.pathname === '/tools' ? 'ui/tools.html' : u.pathname.slice(1));
      return route.fulfill({ body: await fs.readFile(file), contentType: { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.webp': 'image/webp' }[path.extname(file)] });
    }
    if (u.pathname === '/api/build_f/tools/connect') {
      calls.push('connect');
      state.servers.fixture.connection_state = 'authorizing';
      state.servers.fixture.authorization = { url: 'https://auth.example/authorize?state=fixture', issuer: 'auth.example', scope: 'read' };
      return route.fulfill({ json: state });
    }
    if (u.pathname === '/api/build_f/tools/disconnect') {
      calls.push('disconnect');
      state.servers.fixture.connection_state = 'disconnected';
      state.servers.fixture.authorization = null;
      state.servers.fixture.connected = false;
      state.servers.fixture.enabled = false;
      return route.fulfill({ json: state });
    }
    if (u.pathname === '/api/build_f/tools') return route.fulfill({ json: state });
    if (u.pathname === '/api/build_f/tools/config') { calls.push('save'); return route.fulfill({ json: state }); }
    if (u.pathname.startsWith('/api/build_f/tools/attachments/')) return route.fulfill({ body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aC1cAAAAASUVORK5CYII=', 'base64'), contentType: 'image/png' });
    return route.fulfill({ json: { ok: true, enabled: false, servers: {}, iot_disclaimer: state.iot_disclaimer } });
  });
  await page.goto('http://localhost/tools');
  await page.locator('[data-connect]').waitFor();
  return { page, state, calls };
}

for (const width of [1280, 390]) {
  test(`tools connection and image flow at ${width}px`, async t => {
    const { page, state, calls } = await open(t, width);
    const connect = page.locator('[data-connect="fixture"]');
    assert.equal(await connect.textContent(), 'Sign in');
    await connect.click();
    await page.locator('[data-auth]').waitFor();
    assert.equal(await connect.isDisabled(), true);
    assert.deepEqual(calls, ['connect']);
    assert.equal(await page.locator('[data-auth]').getAttribute('target'), '_blank');
    await page.locator('[data-auth]').focus();
    await page.waitForTimeout(5200); // One real status poll must preserve focused sign-in link.
    assert.equal(await page.locator('[data-auth]').evaluate(el => el === document.activeElement), true);
    await page.locator('[data-disconnect]').click();
    await page.locator('[data-auth]').waitFor({ state: 'hidden' });
    assert.deepEqual(calls, ['connect', 'disconnect']);
    assert.equal(await page.locator('[data-auth]').count(), 0);
    state.servers.fixture.connected = true;
    state.servers.fixture.enabled = true;
    state.servers.fixture.connection_state = 'connected';
    state.servers.fixture.protocol_version = '2026-07-28';
    const id = 'a'.repeat(32);
    state.results = [{ id: 'r', server: 'fixture', tool: 'get_image', content: [{ type: 'image', id,
      url: '/api/build_f/tools/attachments/' + id, analysis: 'A small test image.' }] }];
    await page.reload();
    await page.locator('#toolResults summary').click();
    const img = page.locator('#toolResults img');
    await img.waitFor({ state: 'visible' });
    await img.evaluate(el => el.decode());
    assert.match(await page.locator('#toolResults').textContent(), /A small test image/);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await fs.mkdir(path.join(root, '.venv', 'artifacts'), { recursive: true });
    await page.locator('#serversBox').scrollIntoViewIfNeeded();
    await page.waitForFunction(() => Number(getComputedStyle(document.getElementById('serversBox')).opacity) === 1);
    await page.locator('#serversBox').screenshot({ path: path.join(root, '.venv', 'artifacts', `connection-${width}.png`) });
    for (const section of await page.locator('.rv').all()) {
      if (await section.isVisible()) { await section.scrollIntoViewIfNeeded(); await page.waitForTimeout(120); }
    }
    await page.evaluate(() => scrollTo(0, 0));
    await page.waitForTimeout(600);
    await page.screenshot({ path: path.join(root, '.venv', 'artifacts', `tools-${width}.png`), fullPage: true });
  });
}
