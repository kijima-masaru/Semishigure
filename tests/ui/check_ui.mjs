// UI checks with Playwright + axe-core: no horizontal scroll at 375 / 768 / 1440 px,
// no page errors, no serious/critical accessibility violations, on every tab.
// Usage: node tests/ui/check_ui.mjs http://127.0.0.1:8080 [screenshot dir]
import { chromium } from 'playwright';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const base = process.argv[2] || 'http://127.0.0.1:8080', out = process.argv[3] || '';
const axeSource = readFileSync(require.resolve('axe-core/axe.min.js'), 'utf8');
const launch = { args: ['--no-sandbox'] };
if (process.env.PW_CHROMIUM) launch.executablePath = process.env.PW_CHROMIUM;
const browser = await chromium.launch(launch);
let failures = 0;
const fail = (m) => { failures++; console.log('FAIL ' + m); };
for (const w of [1440, 768, 375]) {
  const page = await browser.newPage({ viewport: { width: w, height: 900 } });
  page.on('pageerror', (e) => fail(`pageerror @${w}: ${e.message.slice(0, 200)}`));
  page.on('console', (m) => { if (m.type() === 'error' && !/ERR_TUNNEL_CONNECTION_FAILED|ERR_NAME_NOT_RESOLVED|cdn\.jsdelivr/.test(m.text())) fail(`console @${w}: ${m.text().slice(0, 200)}`); }); // the CDN copy of Vue may be unreachable; the vendored fallback is used then
  await page.goto(base + '/#run', { waitUntil: 'networkidle' });
  await page.waitForSelector('nav.tabs button');
  for (const t of ['実行', 'シナリオ', 'PBX', '結果']) {
    await page.getByRole('button', { name: t, exact: true }).click();
    await page.waitForTimeout(500);
    if (t === '結果') { const b = page.getByRole('button', { name: /の詳細$/ }).first(); if (await b.count()) { await b.click(); await page.waitForTimeout(800); } }
    const sw = await page.evaluate(() => document.documentElement.scrollWidth);
    if (sw > w) fail(`horizontal overflow @${w} ${t}: scrollWidth ${sw}`); else console.log(`ok  ${t} @${w}: no horizontal scroll`);
    await page.evaluate(axeSource);
    const res = await page.evaluate(async () => await window.axe.run(document, { runOnly: ['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa'] }));
    const bad = res.violations.filter((v) => ['serious', 'critical'].includes(v.impact));
    for (const v of bad) fail(`axe @${w} ${t}: ${v.id} (${v.impact}) ${v.nodes.length} nodes: ${v.nodes[0].target.join(' ')} — ${v.help}`);
    if (!bad.length) console.log(`ok  ${t} @${w}: axe ${res.violations.length} minor/moderate, 0 serious`);
    if (out) await page.screenshot({ path: `${out}/${t}_${w}.png`, fullPage: true });
  }
  await page.close();
}
await browser.close();
if (failures) { console.log(`${failures} failure(s)`); process.exit(1); }
console.log('all checks passed');
