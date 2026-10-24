// Opt-in integration test: creates/deletes only its own offline pager session.
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const engine = process.env.BROWSER_ENGINE || 'webkit';
const browserType = require(process.env.PLAYWRIGHT_PATH || 'playwright')[engine];
const base = process.env.BRIDGE_URL || 'http://127.0.0.1:4601';
const server = process.env.BRIDGE_SERVER || 'test-node';
const name = 'bridge-fullscreen-scroll-' + Date.now() + '-' + process.pid;
const quote = s => "'" + s.replace(/'/g, "'\\''") + "'";
async function api(method, body) {
  const response = await fetch(base + '/api/' + server + '/sessions/' + name, {
    method, headers: {'Content-Type': 'application/json'}, ...(body ? {body: JSON.stringify(body)} : {}),
  });
  const data = await response.json(); assert(response.ok, JSON.stringify(data)); return data;
}
(async () => {
  let browser, created = false;
  try {
    const program = fs.readFileSync(path.join(__dirname, 'fixtures/fullscreen_pager.py'), 'utf8');
    assert((await api('POST', {command: 'python3 -u -c ' + quote(program)})).created); created = true;
    browser = await browserType.launch({headless: true});
    const context = await browser.newContext({viewport: {width: 375, height: 667}, isMobile: true, hasTouch: true});
    const page = await context.newPage(), errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.goto(base, {waitUntil: 'networkidle'});
    await page.evaluate(async ({server, name}) => { enterServer(server); await loadSessions(); connect(name); }, {server, name});
    await page.waitForFunction(() => hasInput && localHistory().includes('PAGER_READY'), {}, {timeout: 20000});
    await page.evaluate(() => term.focus());
    await page.keyboard.type('saved-draft');
    await page.waitForFunction(() => localHistory().includes('DRAFT=saved-draft'));
    const cdp = engine === 'chromium' ? await context.newCDPSession(page) : null;
    async function touch(type, x, y) {
      if (cdp) return cdp.send('Input.dispatchTouchEvent', {type, touchPoints: type === 'touchEnd' ? [] : [{x, y}]});
      await page.evaluate(({type, x, y}) => {
        if (type === 'touchStart') window.swipeTarget = document.elementFromPoint(x, y);
        const touches = type === 'touchEnd' ? [] : [{identifier: 1, target: window.swipeTarget, clientX: x, clientY: y}];
        const event = new Event(type.toLowerCase(), {bubbles: true, cancelable: true});
        Object.defineProperties(event, {touches: {value: touches}, changedTouches: {value: touches}});
        window.swipeTarget.dispatchEvent(event);
      }, {type, x, y});
    }
    async function swipe(start, end) {
      await touch('touchStart', 170, start);
      for (let i = 1; i <= 10; i++) {
        await touch('touchMove', 170, start + (end - start) * i / 10);
        await page.waitForTimeout(25);
      }
      await touch('touchEnd', 170, end);
      await page.waitForTimeout(350);
    }
    await swipe(210, 330);
    await page.waitForFunction(() => localHistory().includes('APPLICATION_PAGE=1'), {}, {timeout: 10000});
    assert(await page.evaluate(() => remoteHistoryPane === null));
    console.log('PASS swipe shows older full-screen application content with empty tmux history');
    await swipe(210, 330);
    await page.waitForFunction(() => localHistory().includes('APPLICATION_PAGE=2'));
    await swipe(330, 210);
    await page.waitForFunction(() => localHistory().includes('APPLICATION_PAGE=1'));
    await swipe(330, 210);
    await page.waitForFunction(() => localHistory().includes('APPLICATION_PAGE=0'));
    await page.evaluate(() => term.focus());
    await page.keyboard.type('-after');
    await page.waitForFunction(() => localHistory().includes('DRAFT=saved-draft-after'));
    assert(await page.evaluate(() => localHistory().includes('ARROWS=0')));
    assert.deepEqual(errors, []);
    console.log('PASS repeated swipes page both directions without recalling prompts; draft and subsequent typing preserved');
  } finally {
    if (browser) await browser.close();
    if (created) await api('DELETE');
  }
})().catch(e => { console.error(e); process.exitCode = 1; });
