// Browser DOM regression using previously observed semantic trees.
// This is a layout-only fixture replay, NEVER app execution/screenshot acceptance.
import { readFileSync, statSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';

const reportPath = process.argv[2];
if (!reportPath || statSync(reportPath).size > 8 * 1024 * 1024) throw new Error('bounded capture report required');
const report = JSON.parse(readFileSync(reportPath, 'utf8'));
const fixtures = report.apps.map(app => ({
  app_id: app.app_id, display_name: app.display_name,
  surface: app.daemon_status.outcome.value.surfaces.find(surface => surface.state === 'open').trusted_surface,
}));
if (!fixtures.length || fixtures.length > 32) throw new Error('bounded fixtures required');
const code = `async (page) => {
  await page.addInitScript(() => { window.__VIBAPP_UI_TEST__ = true; });
  await page.goto('http://127.0.0.1:4190');
  const fixtures = ${JSON.stringify(fixtures)};
  const checks = [];
  for (const fixture of fixtures) {
    for (const width of [375, 520, 900]) {
      await page.setViewportSize({ width, height: 720 });
      const measured = await page.evaluate(fixture => {
        const ui = window.VibAppUiTest;
        document.body.classList.add('runtime-host-window');
        ui.model.runtimeWindowAppId = fixture.app_id;
        ui.model.runningApp = { descriptor: { id: fixture.app_id, display_name: fixture.display_name },
          surface: fixture.surface, launcher_context: { installed: true } };
        document.querySelector('#view').innerHTML = ui.renderRuntime();
        return ui.runtimeInitialContentHeight();
      }, fixture);
      const height = Math.min(900, Math.max(240, measured || 300));
      await page.setViewportSize({ width, height });
      checks.push(await page.evaluate(({id,width,height}) => {
        const controls = [...document.querySelectorAll('.guest-surface button, .guest-surface input, .guest-surface select, .guest-surface textarea')];
        const violations = controls.flatMap(control => {
          const rect = control.getBoundingClientRect();
          return rect.x < -0.5 || rect.right > width + 0.5 || rect.width < 43.5 || rect.height < 43.5
            ? [{ label: control.textContent.slice(0,50), rect: rect.toJSON() }] : [];
        });
        const offscreen = controls.filter(control => control.getBoundingClientRect().bottom > height - 48).length;
        return {id,width,height,controls:controls.length,offscreen,violations,
          horizontalOverflow: document.querySelector('main').scrollWidth > width};
      }, {id:fixture.app_id,width,height}));
    }
  }
  return {kind:'synthetic-layout-replay-not-app-execution',checks};
}`;
const output = execFileSync('/Users/zhuzhe/.codex/skills/playwright/scripts/playwright_cli.sh',
  ['-s=runtime-layout', 'run-code', code], { encoding: 'utf8', timeout: 45000, maxBuffer: 2 * 1024 * 1024 });
const payload = output.match(/### Result\n([^\n]+)\n/);
if (!payload) throw new Error(output.slice(0,2000));
const result = JSON.parse(payload[1]);
process.stdout.write(JSON.stringify(result, null, 2) + '\n');
if (process.argv[3]) writeFileSync(process.argv[3], JSON.stringify(result, null, 2) + '\n', {flag:'wx', mode:0o600});
if (result.checks.some(check => check.violations.length || check.horizontalOverflow)) process.exitCode = 1;
