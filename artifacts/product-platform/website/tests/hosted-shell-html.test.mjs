import assert from 'node:assert/strict';
import { test } from 'node:test';
import { HOSTED_SHELL_RESOURCE_CSP, prepareHostedShellHtml } from '../scripts/hosted-shell-html.mjs';

test('hosted shell carries resource CSP before loads, with no duplicate policy', () => {
  for (const language of ['en', 'en-US', 'zh-CN']) {
    const input = '<html lang="' + language + '"><head><meta charset="utf-8"><link href="styles.css"><script src="app.js"></script></head></html>';
    const output = prepareHostedShellHtml(input);
    assert.ok(output.includes('data-vibapp-hosted-shell="true"'));
    assert.ok(output.indexOf('Content-Security-Policy') < output.indexOf('<link'));
    assert.ok(output.includes(HOSTED_SHELL_RESOURCE_CSP));
    assert.equal(prepareHostedShellHtml(output), output);
  }
  for (const directive of ["worker-src 'self'", "frame-src 'none'", "connect-src 'self'", "script-src 'self' 'wasm-unsafe-eval'"]) {
    assert.ok(HOSTED_SHELL_RESOURCE_CSP.includes(directive));
  }
  assert.ok(!HOSTED_SHELL_RESOURCE_CSP.includes('frame-ancestors'));
});
test('unexpected shell markup fails closed', () => {
  assert.throws(() => prepareHostedShellHtml('<html><head></head></html>'));
  assert.throws(() => prepareHostedShellHtml('<html lang="en"><head></head></html>'));
});
