#!/bin/sh
set -eu

playwright_cli=${VIBAPP_PLAYWRIGHT_CLI:?set VIBAPP_PLAYWRIGHT_CLI to the absolute playwright_cli.sh path}
website_url=${VIBAPP_WEBSITE_URL:-http://127.0.0.1:3000}
session=${VIBAPP_PLAYWRIGHT_SESSION:-vibapp-web-intake-acceptance}

case "$playwright_cli" in
  /*) ;;
  *) echo "VIBAPP_PLAYWRIGHT_CLI must be absolute" >&2; exit 64 ;;
esac

cleanup() {
  "$playwright_cli" -s="$session" close >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM

"$playwright_cli" -s="$session" open "$website_url/" --headed >/dev/null
"$playwright_cli" -s="$session" run-code 'async page => {
  const errors = [];
  page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
  await page.evaluate(() => {
    localStorage.removeItem("vibapp.ui_locale");
    localStorage.removeItem("vibapp.web-launcher.state.v1");
  });
  await page.reload();
  const waitForLauncher = async () => {
    for (let attempt = 0; attempt < 80; attempt += 1) {
      const frame = page.frames().find(candidate => candidate.url().includes("/launcher/index.html"));
      if (frame) return frame;
      await page.waitForTimeout(100);
    }
    throw new Error("shared launcher frame did not load");
  };
  const frame = await waitForLauncher();
  const state = () => frame.evaluate(() => window.VibAppWebBridge.invoke("get_state"));
  const waitForNeedCount = async expected => {
    for (let attempt = 0; attempt < 80; attempt += 1) {
      if ((await state()).needs.length === expected) return;
      await page.waitForTimeout(100);
    }
    throw new Error(`need count did not reach ${expected}`);
  };
  const textarea = frame.locator("form.composer textarea[name=description]");
  const submit = frame.locator("form.composer button[type=submit]");
  const clickNeed = "Build a tiny click-submitted checklist for exact browser acceptance.";
  const enterNeed = "Build a tiny Enter-submitted notes tool for exact browser acceptance.";
  await textarea.fill(clickNeed);
  await submit.click();
  await waitForNeedCount(1);
  if (await textarea.inputValue() !== "") throw new Error("click submission did not clear the composer");
  await textarea.fill(enterNeed);
  await textarea.press("Enter");
  await waitForNeedCount(2);
  await page.waitForTimeout(300);
  const beforeReload = await state();
  if (beforeReload.needs.length !== 2) throw new Error("trusted submission duplicated a need");
  await page.reload();
  const reloadedFrame = await waitForLauncher();
  const afterReload = await reloadedFrame.evaluate(() => window.VibAppWebBridge.invoke("get_state"));
  if (afterReload.needs.length !== 2) throw new Error("submitted needs did not survive reload");
  if (afterReload.needs[0].description !== clickNeed || afterReload.needs[1].description !== enterNeed) {
    throw new Error("persisted needs drifted from trusted user input");
  }
  if (errors.length) throw new Error("browser console errors: " + errors.join(" | "));
  const result = {
    status: "PASS",
    trusted_click_submissions: 1,
    trusted_enter_submissions: 1,
    duplicate_submissions: 0,
    reload_persisted_needs: 2,
    console_errors: 0,
  };
  await page.evaluate(() => {
    localStorage.removeItem("vibapp.ui_locale");
    localStorage.removeItem("vibapp.web-launcher.state.v1");
  });
  await page.reload();
  return JSON.stringify(result);
}'
