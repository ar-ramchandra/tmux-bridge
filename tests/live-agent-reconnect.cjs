// Opt-in: waits for an operator to interrupt the selected node's transport.
// Creates/deletes only its own disposable session; does not stop any agent.
const assert = require('node:assert/strict');
const engine = process.env.BROWSER_ENGINE || 'chromium';
const browserType = require(process.env.PLAYWRIGHT_PATH || 'playwright')[engine];
const base = process.env.BRIDGE_URL || 'http://127.0.0.1:4601';
const server = process.env.BRIDGE_SERVER || 'test-node';
const name = 'bridge-agent-reconnect-' + Date.now() + '-' + process.pid;
const quote = s => "'" + s.replace(/'/g, "'\\''") + "'";
async function api(method, body) {
  const r = await fetch(base + '/api/' + server + '/sessions/' + name, {
    method, headers: {'Content-Type': 'application/json'},
    ...(body ? {body: JSON.stringify(body)} : {}),
  });
  const data = await r.json(); assert(r.ok, JSON.stringify(data)); return data;
}
(async () => {
  let browser, created = false;
  try {
    const command = 'python3 -u -c ' + quote("import os,tty\ntty.setraw(0)\nos.write(1,b'RECONNECT_READY\\r\\n')\nwhile True:\n data=os.read(0,4096)\n if not data:break\n os.write(1,data)\n");
    assert((await api('POST', {command})).created); created = true;
    browser = await browserType.launch({headless: true});
    const page = await browser.newPage({viewport: {width: 1280, height: 800}});
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    await page.goto(base, {waitUntil: 'networkidle'});
    await page.evaluate(async ({server, name}) => {
      enterServer(server); await loadSessions(); connect(name);
    }, {server, name});
    await page.waitForFunction(() => hasInput && localHistory().includes('RECONNECT_READY'), {}, {timeout: 20000});
    await page.keyboard.type('BEFORE_DROP');
    await page.waitForFunction(() => localHistory().includes('BEFORE_DROP'));
    await page.evaluate(() => window.socketBeforeDrop = ws);
    console.log('READY: interrupt the selected test node transport now');
    await page.waitForFunction(() => window.socketBeforeDrop.readyState === WebSocket.CLOSED, {}, {timeout: 90000});
    await page.waitForFunction(() => ws !== window.socketBeforeDrop && ws.readyState === WebSocket.OPEN && hasInput, {}, {timeout: 30000});
    // No click, refresh, or manual control transfer after reconnection.
    assert(await page.evaluate(() => document.activeElement === term.textarea));
    await page.keyboard.type('AFTER_DROP_INPUT_OK');
    await page.waitForFunction(() => localHistory().includes('AFTER_DROP_INPUT_OK'), {}, {timeout: 10000});
    assert.deepEqual(errors, []);
    console.log('PASS browser automatically reattached and real keyboard input reached the terminal after agent reconnection');
  } finally {
    if (browser) await browser.close();
    if (created) await api('DELETE');
  }
})().catch(e => { console.error(e); process.exitCode = 1; });
