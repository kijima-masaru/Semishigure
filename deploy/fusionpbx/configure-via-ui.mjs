import { chromium } from '/opt/node22/lib/node_modules/playwright/index.mjs';
const base = 'http://127.0.0.1:8090', out = process.argv[2];
const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium', args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });
page.on('pageerror', e => console.log('pageerror:', e.message.slice(0, 200)));
await page.goto(base + '/login.php', { waitUntil: 'networkidle' });
await page.fill('input[name=username]', 'admin@pbx.semishigure.test');
await page.fill('input[name=password]', process.env.FUSIONPBX_ADMIN_PW || 'semishigure-admin');
await Promise.all([page.waitForNavigation({ waitUntil: 'networkidle' }), page.click('button[type=submit], input[type=submit]')]);
for (const ext of ['9100', '9001', '9002', '9003', '9004']) {
  await page.goto(base + '/app/extensions/extension_edit.php', { waitUntil: 'networkidle' });
  await page.fill('input[name=extension]', ext);
  await page.fill('input[name=limit_max]', '5');
  await page.fill('input[name=effective_caller_id_name]', ext === '9100' ? 'Caller 9100' : 'Answerer ' + ext);
  await page.fill('input[name=effective_caller_id_number]', ext);
  await Promise.all([page.waitForNavigation({ waitUntil: 'networkidle' }), page.click('button:has-text("SAVE"), button:has-text("Save")')]);
  console.log('extension', ext, '->', page.url().replace(base, '').slice(0, 60));
}
await page.goto(base + '/app/extensions/extensions.php', { waitUntil: 'networkidle' });
await page.screenshot({ path: out + '/fusion_extensions.png', fullPage: true });
await page.goto(base + '/app/ring_groups/ring_group_edit.php', { waitUntil: 'networkidle' });
await page.fill('input[name=ring_group_name]', 'loadtest');
await page.fill('input[name=ring_group_extension]', '8001');
const strat = page.locator('select[name=ring_group_strategy]');
console.log('strategy options:', await strat.locator('option').allInnerTexts());
await strat.selectOption({ label: 'Simultaneous' }).catch(async () => strat.selectOption('simultaneous'));
const users = ['9001', '9002', '9003', '9004'];
for (let i = 0; i < users.length; i++) {
  let row = page.locator(`input[name="ring_group_destinations[${i}][destination_number]"]`);
  if (!(await row.count())) { await page.click('button:has-text("ADD"), button:has-text("Add")'); await page.waitForTimeout(400); row = page.locator(`input[name="ring_group_destinations[${i}][destination_number]"]`); }
  await row.fill(users[i]);
}
await page.fill('input[name=ring_group_call_timeout]', '30');
await page.screenshot({ path: out + '/fusion_ring_group_form.png', fullPage: true });
await Promise.all([page.waitForNavigation({ waitUntil: 'networkidle' }), page.click('button:has-text("SAVE"), button:has-text("Save")')]);
console.log('ring group ->', page.url().replace(base, '').slice(0, 60));
await page.goto(base + '/app/ring_groups/ring_groups.php', { waitUntil: 'networkidle' });
await page.screenshot({ path: out + '/fusion_ring_groups.png', fullPage: true });
await browser.close();
