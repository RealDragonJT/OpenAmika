const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const fs = require('node:fs/promises');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
let browser;
before(async () => {
  browser = await chromium.launch({ args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'] });
});
after(async () => { await browser?.close(); });

// Serve the real UI through routing: no server, models, account, or running stack.
// Media uses Chromium's synthetic devices. API responses and dialog sockets are fixtures.
async function open(t, viewport = { width: 1440, height: 900 }, deviceScaleFactor = 1, readyAvatar = false, options = {}) {
  const context = await browser.newContext({ viewport, deviceScaleFactor, permissions: ['camera', 'microphone'] });
  t.after(() => context.close());
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  t.after(() => assert.deepEqual(errors, [], 'no browser script errors'));
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    const p = url.pathname;
    if (url.hostname !== 'localhost') return route.fulfill({ body: '', contentType: 'text/css' });
    if (p === '/use' || p.startsWith('/ui/')) {
      const f = path.join(root, p === '/use' ? 'ui/use.html' : p.slice(1));
      const contentType = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png', '.webp': 'image/webp' }[path.extname(f)];
      if (readyAvatar && p === '/ui/live2d-engine.js') {
        // Simulate a loaded model and observe the busy-display API without licensed model assets.
        const body = await fs.readFile(f, 'utf8') + `
          OpenAmikaAvatar.init = async options => { fixture.avatarOptions = options; return { ok: true }; };
          fixture.busyHistory = [];
          OpenAmikaAvatar.setBusy = on => { fixture.busy = on; fixture.busyHistory.push(on); };
          const flush = OpenAmikaAvatar.flush;
          fixture.flushCount = 0;
          OpenAmikaAvatar.flush = () => { fixture.flushCount++; flush(); };
        `;
        return route.fulfill({ body, contentType });
      }
      return route.fulfill({ body: await fs.readFile(f), contentType });
    }
    const fixtures = {
      '/api/build_f/persona': { onboarded: true },
      '/api/stage': { blur: 12, background: null },
      '/api/avatar/models': { models: [], emotions: [] },
      '/api/vision': { enabled: true },
    };
    return route.fulfill({ json: fixtures[p] || {} });
  });
  await page.addInitScript(({ host }) => {
    window.fixture = { sockets: [], streams: [], requests: [], pending: [], holdMedia: false, denyMedia: false };
    if (host) {
      fixture.hostCalls = [];
      window.pywebview = { api: Object.fromEntries(['zoom', 'full_page', 'close'].map(name =>
        [name, (...args) => { fixture.hostCalls.push([name, ...args]); return Promise.resolve(); }])) };
    }
    window.WebSocket = class {
      constructor(url) {
        this.url = url; this.readyState = 0; this.bufferedAmount = 0; this.sent = [];
        fixture.sockets.push(this);
        setTimeout(() => { if (this.readyState === 0) { this.readyState = 1; this.onopen?.({}); } }, 0);
      }
      send(data) { this.sent.push(data); }
      close() { this.readyState = 3; setTimeout(() => this.onclose?.({}), 40); }
    };
    const get = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = async constraints => {
      fixture.requests.push(constraints);
      if (fixture.holdMedia) await new Promise(resolve => fixture.pending.push(resolve));
      if (fixture.denyMedia) throw new DOMException('Test permission refusal', 'NotAllowedError');
      const stream = await get(constraints);
      fixture.streams.push(stream);
      return stream;
    };
  }, { host: !!options.host });
  await page.goto('http://localhost:53999/use?' + (options.pet ? 'pet=1' : 'kiosk=1'));
  await page.waitForFunction(ready => window.OA && window.OpenAmikaAvatar &&
    (ready ? Array.isArray(fixture.busyHistory) && fixture.busyHistory.length : !document.querySelector('#stageEmpty').hidden), readyAvatar);
  return page;
}
const pressed = (page, id, value) => page.waitForFunction(([id, value]) => document.getElementById(id).getAttribute('aria-pressed') === String(value), [id, value]);
const noTracks = page => page.waitForFunction(() => fixture.streams.every(s => s.getTracks().every(t => t.readyState === 'ended')));

test('scene, expand menu, and transcript dismiss with focus restored; draft text survives', async t => {
  const p = await open(t);
  await p.locator('#sceneBtn').click();
  await p.locator('#bgBlur').focus();
  await p.keyboard.press('Escape');
  assert.equal(await p.locator('#scenePanel').isHidden(), true);
  assert.equal(await p.locator('#sceneBtn').evaluate(e => e === document.activeElement), true);
  await p.locator('#fsBtn').click();
  await p.keyboard.press('Escape');
  assert.equal(await p.locator('#expandMenu').isHidden(), true);
  await p.locator('#fsBtn').click();
  await p.locator('[data-mode="window"]').click();
  await p.locator('#sayInput').fill('Keep this draft');
  await p.keyboard.press('Escape');
  assert.equal(await p.locator('#stageArea').evaluate(e => e.classList.contains('u-big')), false);
  assert.equal(await p.locator('#sayInput').inputValue(), 'Keep this draft');
  await p.locator('#fsBtn').click();
  await p.locator('[data-mode="window"]').click();
  await p.locator('#drawerBtn').click();
  await p.keyboard.press('Escape');
  assert.equal(await p.locator('#drawer').isHidden(), true);
  assert.equal(await p.locator('#drawerBtn').evaluate(e => e === document.activeElement), true);
});

test('rapid reconnect ignores stale socket callbacks and sends text once', async t => {
  const p = await open(t);
  await p.locator('#powerBtn').click();
  await pressed(p, 'powerBtn', true);
  await p.waitForFunction(() => fixture.sockets[0].readyState === 1);
  await p.evaluate(() => { powerBtn.click(); powerBtn.click(); });
  await p.waitForFunction(() => fixture.sockets[1]?.readyState === 1);
  await p.evaluate(() => fixture.sockets[0].onclose({}));
  await p.locator('#sayInput').fill('Hello');
  assert.equal(await p.locator('#sayBtn').isEnabled(), true);
  await p.locator('#sayBtn').click();
  assert.equal(await p.evaluate(() => fixture.sockets[1].sent.filter(s => typeof s === 'string' && JSON.parse(s).type === 'text').length), 1);
  assert.equal(await p.locator('#sayInput').inputValue(), '');
  await p.locator('#sayInput').fill('Retain on reconnect');
  await p.evaluate(() => { fixture.sockets[1].readyState = 3; fixture.sockets[1].onclose({}); });
  assert.equal(await p.locator('#sayBtn').isDisabled(), true);
  await p.waitForFunction(() => fixture.sockets[2]?.readyState === 1);
  assert.equal(await p.locator('#sayInput').inputValue(), 'Retain on reconnect');
});

test('disconnect clears thinking immediately and cancels its timer without affecting a new socket', async t => {
  const p = await open(t, undefined, 1, true);
  await p.clock.install();
  await p.locator('#powerBtn').click();
  await p.waitForFunction(() => fixture.sockets[0]?.readyState === 1);
  const think = socket => p.evaluate(index => fixture.sockets[index].onmessage({
    data: JSON.stringify({ type: 'ai_surface_done', mode: 'think', surface_tts_done: true }),
  }), socket);
  await think(0);
  assert.equal(await p.evaluate(() => fixture.busy), true, 'waiting for the deep reply shows thinking');
  await p.locator('#powerBtn').click();
  assert.equal(await p.evaluate(() => fixture.busy), false, 'Power off clears thinking immediately');
  assert.equal(await p.locator('#powerState').textContent(), 'Offline');
  const historyLength = await p.evaluate(() => fixture.busyHistory.length);
  await p.clock.fastForward(120001);
  assert.equal(await p.evaluate(() => fixture.busyHistory.length), historyLength, 'the canceled safety timer never fires');
  assert.equal(await p.evaluate(() => fixture.sockets.length), 1, 'explicit disconnect does not reconnect');
  await p.locator('#powerBtn').click();
  await p.waitForFunction(() => fixture.sockets[1]?.readyState === 1);
  await think(1);
  await p.evaluate(() => fixture.sockets[0].onclose({}));
  assert.equal(await p.evaluate(() => fixture.busy), true, 'old close cannot clear thinking on the new connection');
  await p.evaluate(() => fixture.sockets[1].onmessage({ data: JSON.stringify({ type: 'ai_deep_done' }) }));
  assert.equal(await p.evaluate(() => fixture.busy), false, 'the new reply clears its own thinking state');
});

test('camera off cancels a pending permission request and stops its late stream', async t => {
  const p = await open(t);
  await p.evaluate(() => { fixture.holdMedia = true; });
  await p.locator('#camBtn').click();
  await p.waitForFunction(() => fixture.pending.length === 1);
  await p.locator('#camBtn').click();
  await p.evaluate(() => fixture.pending.shift()());
  await p.waitForFunction(() => fixture.streams.length === 1);
  await noTracks(p);
  await pressed(p, 'camBtn', false);
  assert.equal(await p.locator('#camPip').isHidden(), true);
});

test('camera restart ignores an old socket closing; preview hiding keeps camera on', async t => {
  const p = await open(t);
  await p.locator('#camBtn').click();
  await pressed(p, 'camBtn', true);
  await p.locator('#t-set').click();
  await p.locator('#camSelect').selectOption({ index: 1 });
  await p.waitForFunction(() => fixture.streams.length === 2 && fixture.sockets.length === 2);
  await p.evaluate(() => fixture.sockets[0].onclose({}));
  await pressed(p, 'camBtn', true);
  await p.locator('#camPip').focus();
  await p.keyboard.press('ArrowRight');
  await p.keyboard.press('Enter');
  await p.locator('#pipHideBtn').click();
  assert.equal(await p.locator('#camPip').evaluate(e => e.classList.contains('collapsed')), true);
  assert.equal(await p.evaluate(() => fixture.streams[1].getTracks()[0].readyState), 'live');
  assert.ok(await p.evaluate(() => localStorage.getItem('openamika-camPipPos')));
  await p.locator('#camBtn').click();
  await noTracks(p);
  await p.reload();
  assert.equal(await p.locator('#camPip').evaluate(e => e.classList.contains('collapsed')), true);
});

test('disconnect cancels pending microphone capture; permission denial can be retried', async t => {
  const p = await open(t);
  await p.evaluate(() => { fixture.holdMedia = true; });
  await p.locator('#micBtn').click();
  await p.waitForFunction(() => fixture.pending.length === 1);
  await p.locator('#powerBtn').click();
  await p.evaluate(() => fixture.pending.shift()());
  await p.waitForFunction(() => fixture.streams.length === 1);
  await noTracks(p);
  await pressed(p, 'micBtn', false);
  await p.evaluate(() => { fixture.holdMedia = false; fixture.denyMedia = true; });
  await p.locator('#micBtn').click();
  await p.waitForFunction(() => document.querySelector('.oa-toast')?.textContent.includes('refused'));
  await p.evaluate(() => { fixture.denyMedia = false; });
  await p.locator('#micBtn').click();
  await pressed(p, 'micBtn', true);
  await p.locator('#micBtn').click();
  await noTracks(p);
});

test('an unplugged saved camera falls back to the default without losing its preference', async t => {
  const p = await open(t);
  await p.evaluate(() => localStorage.setItem('openamika-camDevice', 'missing-camera'));
  await p.reload();
  await p.locator('#camBtn').click();
  await pressed(p, 'camBtn', true);
  assert.equal(await p.evaluate(() => fixture.requests.length), 2);
  assert.equal(await p.evaluate(() => localStorage.getItem('openamika-camDevice')), 'missing-camera');
  await p.locator('#camBtn').click();
  await noTracks(p);
});

test('lock persists; shortcuts do not run while typing or repeating', async t => {
  const p = await open(t);
  await p.locator('#lockBtn').click();
  await p.reload();
  await pressed(p, 'lockBtn', true);
  await p.locator('#sayInput').fill('lwmcf');
  await pressed(p, 'lockBtn', true);
  await p.locator('#lockBtn').focus();
  await p.evaluate(() => document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'l', repeat: true, bubbles: true })));
  await pressed(p, 'lockBtn', true);
});

test('full screen enter/exit restores navigation and a blocked request fills the window', async t => {
  const p = await open(t);
  await p.locator('#fsBtn').click();
  await p.locator('[data-mode="fullscreen"]').click();
  await p.waitForFunction(() => document.fullscreenElement?.id === 'stageArea');
  assert.equal(await p.locator('.oa-nav').evaluate(e => e.inert), true);
  await p.locator('#fsBtn').click();
  await p.waitForFunction(() => !document.fullscreenElement && !document.querySelector('.oa-nav').inert);
  await p.evaluate(() => { stageArea.requestFullscreen = () => Promise.reject(new Error('blocked')); });
  await p.locator('#fsBtn').click();
  await p.locator('[data-mode="fullscreen"]').click();
  await p.waitForFunction(() => stageArea.classList.contains('u-big'));
  await p.locator('#sayInput').focus();
  await p.keyboard.press('Escape');
  assert.equal(await p.locator('.oa-nav').evaluate(e => e.inert), false);
});

test('navigation fits through the compact breakpoint without widening the page', async t => {
  const p = await open(t);
  for (const width of [560, 561, 600, 650, 700, 701, 760, 761]) {
    await p.setViewportSize({ width, height: 900 });
    assert.equal(await p.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `no overflow at ${width}px`);
    const stage = await p.locator('#stageArea').boundingBox();
    assert.ok(Math.abs(stage.x - (width - stage.x - stage.width)) < 1, `equal stage margins at ${width}px`);
    await p.locator('.oa-mode button[data-mode="simple"]').click();
    if (width <= 760) {
      assert.equal(await p.evaluate(() => document.documentElement.classList.contains('mode-pro')), true);
      await p.locator('.oa-mode button[data-mode="pro"]').click();
      assert.equal(await p.evaluate(() => document.documentElement.classList.contains('mode-simple')), true);
    }
  }
});

test('compact health indicator is circular and centered, and still opens status details', async t => {
  const p = await open(t);
  for (const width of [390, 561, 700, 760, 761, 1100]) {
    await p.setViewportSize({ width, height: 900 });
    // Read both in one frame: wrapping may move the entire control between two awaited reads.
    const { button, dot } = await p.evaluate(() => ({
      button: navHealth.getBoundingClientRect().toJSON(), dot: navHealthLight.getBoundingClientRect().toJSON(),
    }));
    assert.ok(Math.abs(button.width - button.height) < 1, `circle at ${width}px`);
    assert.ok(Math.abs(button.x + button.width / 2 - dot.x - dot.width / 2) < 1, `centered horizontally at ${width}px`);
    assert.ok(Math.abs(button.y + button.height / 2 - dot.y - dot.height / 2) < 1, `centered vertically at ${width}px`);
    await p.locator('#navHealth').click();
    assert.equal(await p.locator('.oa-health-pop').isVisible(), true);
    await p.keyboard.press('Escape');
    assert.equal(await p.locator('.oa-health-pop').count(), 0);
  }
  await p.setViewportSize({ width: 1101, height: 900 });
  assert.equal(await p.locator('#navHealthLabel').isVisible(), true);
});

test('links wrap only when the active mode needs the space, and wrapped links are centered', async t => {
  const p = await open(t);
  const firstSingleRow = {};
  for (const mode of ['simple', 'pro']) {
    await p.setViewportSize({ width: 760, height: 900 });
    if (!(await p.evaluate(mode => document.documentElement.classList.contains('mode-' + mode), mode))) {
      await p.locator('.oa-mode button.active').click();
    }
    for (let width = 320; width <= 760; width += 10) {
      await p.setViewportSize({ width, height: 900 });
      await p.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      const layout = await p.evaluate(() => {
        const links = [...document.querySelectorAll('.oa-nav-links a')].filter(e => e.getBoundingClientRect().width);
        const boxes = links.map(e => e.getBoundingClientRect());
        const brand = document.querySelector('.oa-wordmark').getBoundingClientRect();
        return { wrapped: boxes[0].top >= brand.bottom, center: (boxes[0].left + boxes.at(-1).right) / 2,
          overflow: document.documentElement.scrollWidth > innerWidth };
      });
      assert.equal(layout.overflow, false, `${mode} at ${width}px`);
      if (layout.wrapped) assert.ok(Math.abs(layout.center - width / 2) < 1, `centered links: ${mode} at ${width}px`);
      else firstSingleRow[mode] ??= width;
    }
    assert.ok(firstSingleRow[mode] < 760, `${mode} stays on one row before 760px`);
  }
  assert.ok(firstSingleRow.simple < firstSingleRow.pro, 'shorter Simple label leaves room for links sooner');
  // Resize is not required: changing the mode itself must recompute the layout.
  await p.setViewportSize({ width: firstSingleRow.simple, height: 900 });
  await p.locator('.oa-mode button.active').click();
  assert.equal(await p.locator('.oa-nav').evaluate(e => e.classList.contains('oa-nav-wrapped')), false);
  await p.locator('.oa-mode button.active').click();
  assert.equal(await p.locator('.oa-nav').evaluate(e => e.classList.contains('oa-nav-wrapped')), true);
});

test('painted health dot and ring share a center at different display scales', async t => {
  for (const scale of [1, 1.25, 1.5, 2]) {
    const p = await open(t, { width: 650, height: 900 }, scale);
    await p.waitForFunction(() => navHealthLabel.textContent !== 'Checking…');
    await p.addStyleTag({ content: '.oa-health .light { transition: none; }' });
    await p.locator('#navHealthLight').evaluate(e => { e.className = 'light ok'; });
    const b = await p.locator('#navHealth').boundingBox();
    const shot = await p.screenshot({ clip: { x: Math.floor(b.x) - 4, y: Math.floor(b.y) - 4, width: Math.ceil(b.width) + 8, height: Math.ceil(b.height) + 8 } });
    // Decode the actual screenshot in canvas: DOM rectangles alone miss paint-time snapping.
    const centers = await p.evaluate(async base64 => {
      const image = new Image(); image.src = 'data:image/png;base64,' + base64; await image.decode();
      const canvas = document.createElement('canvas'); canvas.width = image.width; canvas.height = image.height;
      const ctx = canvas.getContext('2d'); ctx.drawImage(image, 0, 0);
      const { data } = ctx.getImageData(0, 0, canvas.width, canvas.height);
      const dot = [], ring = [];
      for (let y = 0; y < canvas.height; y++) for (let x = 0; x < canvas.width; x++) {
        const i = (y * canvas.width + x) * 4, r = data[i], g = data[i + 1], b = data[i + 2];
        if (g > 130 && g > r * 1.5 && g > b * 1.1) dot.push([x, y]);
        if (r > 30 && b > r && r > g && g < 65) ring.push([x, y]);
      }
      const center = pixels => [0, 1].map(axis => (Math.min(...pixels.map(p => p[axis])) + Math.max(...pixels.map(p => p[axis]))) / 2);
      return { dot: center(dot), ring: center(ring), counts: [dot.length, ring.length] };
    }, shot.toString('base64'));
    assert.ok(centers.counts.every(n => n > 0), 'both circles present in screenshot');
    // Fractional scaling can round opposite edges differently by one physical pixel.
    const tolerance = Number.isInteger(scale) ? 0 : 0.5;
    for (const axis of [0, 1]) assert.ok(Math.abs(centers.dot[axis] - centers.ring[axis]) <= tolerance, `painted center at ${scale}x, axis ${axis}: ${JSON.stringify(centers)}`);
    await p.close();
  }
});

for (const [width, height] of [[320, 740], [390, 844], [844, 390], [1024, 768], [1440, 900]]) {
  test(`layout and navigation at ${width}x${height}`, async t => {
    const p = await open(t, { width, height });
    assert.equal(await p.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await p.locator('#t-set').click();
    assert.equal(await p.locator('#tp-set').isVisible(), true);
    await p.keyboard.press('ArrowLeft');
    assert.equal(await p.locator('#tp-chat').isVisible(), true);
    await p.locator('.oa-mode button[data-mode="simple"]').click();
    if (width <= 760) assert.equal(await p.evaluate(() => document.documentElement.classList.contains('mode-pro')), true);
    await p.locator('#fsBtn').click();
    await p.locator('[data-mode="window"]').click();
    const box = await p.locator('#sayInput').boundingBox();
    assert.ok(box.x >= 0 && box.x + box.width <= width && box.y + box.height <= height);
    await p.keyboard.press('Escape');
  });
}

test('pet stage fills its window transparently and keeps only pet controls', async t => {
  const p = await open(t, { width: 320, height: 560 }, 1, true, { pet: true, host: true });
  assert.equal(await p.evaluate(() => fixture.avatarOptions.fixed), true);
  assert.equal(await p.locator('.oa-nav').count(), 0);
  assert.equal(await p.locator('.u-main').isHidden(), true);
  assert.equal(await p.locator('#stageArea').evaluate(e => e.parentElement === document.body), true);
  assert.equal(await p.locator('#stageCanvas').evaluate(e => e.classList.contains('pywebview-drag-region')), true);
  for (const [width, height] of [[320, 560], [160, 240], [480, 800]]) {
    await p.setViewportSize({ width, height });
    const box = await p.locator('#stageArea').boundingBox();
    assert.deepEqual(box, { x: 0, y: 0, width, height });
    for (const selector of ['html', 'body', '#stageArea']) {
      assert.equal(await p.locator(selector).evaluate(e => getComputedStyle(e).backgroundColor), 'rgba(0, 0, 0, 0)');
    }
    for (const selector of ['.u-vignette', '.u-top', '.u-dock', '.u-stage-cam', '.stage-hud']) {
      assert.equal(await p.locator(selector).isHidden(), true, selector + ' hidden in pet');
    }
    await p.locator('#powerBtn').focus();
    const bar = await p.locator('.pet-bar').boundingBox();
    assert.ok(bar.x >= 0 && bar.x + bar.width <= width, 'all controls fit at ' + width);
    for (const button of await p.locator('.pet-bar > button').all()) {
      await p.waitForFunction(e => getComputedStyle(e).opacity === '1', await button.elementHandle());
    }
  }
  // Page-only shortcuts cannot silently turn on the pet's hidden camera or change the browser's model lock.
  const lock = await p.evaluate(() => localStorage.getItem('openamika-stageLocked'));
  for (const key of ['c', 'w', 'f', 'l']) await p.keyboard.press(key);
  assert.equal(await p.evaluate(() => fixture.requests.length), 0);
  assert.equal(await p.locator('#stageArea').evaluate(e => e.classList.contains('u-big')), false);
  assert.equal(await p.evaluate(() => localStorage.getItem('openamika-stageLocked')), lock);
  await p.clock.install();
  await p.clock.fastForward(31 * 60 * 1000);
  assert.equal(await p.locator('#donate-modal').isHidden(), true, 'no donation overlay in the pet');
});

test('pet window controls call the native bridge and mic capture remains visible until stopped', async t => {
  const p = await open(t, { width: 320, height: 560 }, 1, true, { pet: true, host: true });
  await p.locator('#powerBtn').click();
  await p.waitForFunction(() => fixture.sockets[0]?.readyState === 1);
  await p.locator('#stageCanvas').hover({ position: { x: 100, y: 100 } });
  await p.mouse.wheel(0, -100);
  await p.waitForFunction(() => fixture.hostCalls.some(c => c[0] === 'zoom' && c[1] > 1));
  await p.getByRole('button', { name: 'Open the full page (closes this window)', exact: true }).click();
  await p.getByRole('button', { name: 'Close her window (she keeps running)', exact: true }).click();
  assert.equal(await p.evaluate(() => fixture.hostCalls.filter(c => c[0] === 'full_page').length), 1);
  assert.equal(await p.evaluate(() => fixture.hostCalls.filter(c => c[0] === 'close').length), 1);
  await p.keyboard.press('m');
  await pressed(p, 'micBtn', true);
  // Remove hover/focus visibility to verify that an active mic is still shown.
  await p.evaluate(() => document.activeElement.blur());
  await p.mouse.move(-10, -10);
  await p.waitForFunction(() => getComputedStyle(micBtn).opacity === '1');
  await p.keyboard.press('m');
  await pressed(p, 'micBtn', false);
  await noTracks(p);
});

for (const pet of [false, true]) {
  test(`playback acknowledgements and forced-stop recovery in ${pet ? 'pet' : 'browser'}`, async t => {
    const p = await open(t, undefined, 1, true, { pet });
    await p.locator('#powerBtn').click();
    await p.waitForFunction(() => fixture.sockets[0]?.readyState === 1);
    await p.evaluate(() => {
      const meta = { ack: true, turn_id: 'merged-turn', idx: 0, kind: 'deep', text: 'Hello' };
      fixture.avatarOptions.onChunkStart(meta, null);
      fixture.avatarOptions.onChunkEnd(meta);
    });
    const sent = await p.evaluate(() => fixture.sockets[0].sent.filter(x => typeof x === 'string').map(JSON.parse));
    for (const type of ['tts_playing', 'tts_finished']) assert.equal(sent.filter(x => x.type === type && x.turn_id === 'merged-turn').length, 1);
    await p.evaluate(() => fixture.sockets[0].onmessage({ data: JSON.stringify({ type: 'ai_surface_done', mode: 'think', surface_tts_done: true }) }));
    const before = await p.evaluate(() => fixture.flushCount);
    await p.evaluate(() => fixture.sockets[0].onmessage({ data: JSON.stringify({ type: 'stop_audio' }) }));
    assert.equal(await p.evaluate(() => fixture.flushCount), before + 1);
    assert.equal(await p.evaluate(() => fixture.busy), false);
    assert.equal(await p.locator('#hudSay').textContent(), '');
    await p.evaluate(() => fixture.sockets[0].onmessage({ data: JSON.stringify({ type: 'ai_error', provider: 'OpenRouter', message: 'Provider refused this reply' }) }));
    assert.ok((await p.locator('#chatLog').textContent()).includes('Provider refused this reply'));
    await p.locator('#powerBtn').click();
    await pressed(p, 'powerBtn', false);
  });
}

test('shared controls retain saved devices and forward the upstream model-unlock header', async t => {
  const p = await open(t);
  let received;
  await p.route('**/api/build_f/preset', async route => {
    received = { headers: route.request().headers(), body: route.request().postDataJSON() };
    await route.fulfill({ json: { ok: true } });
  });
  await p.evaluate(async () => {
    OA.devices.set('mic', 'chosen-microphone');
    await OA.postAction('/api/build_f/preset', 'apply-preset', { force: true }, { 'X-Build-F-Force': '1' });
  });
  assert.equal(received.headers['x-build-f-force'], '1');
  assert.equal(received.headers['x-build-f-action'], 'apply-preset');
  assert.deepEqual(received.body, { force: true });
  assert.equal(await p.evaluate(() => OA.devices.get('mic')), 'chosen-microphone');
  assert.equal(await p.evaluate(() => OA.keyBalance({ credits: { ok: true, empty: true } }).level), 'err');
});
