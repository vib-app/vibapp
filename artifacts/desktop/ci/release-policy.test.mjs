import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { stampInspectorPin, releaseTag } from './release-policy.mjs';

test('stamp the actual source format, exactly once, before desktop compilation', () => {
  const source = readFileSync(new URL('../../app-builder/descriptor_reconciliation.py', import.meta.url), 'utf8');
  const stamped = stampInspectorPin(source, 'a'.repeat(64));
  assert.match(stamped, new RegExp(`COMPONENT_INSPECTOR_SHA256 = "${'a'.repeat(64)}"`));
  assert.equal(stampInspectorPin(stamped, 'b'.repeat(64)).includes('"' + 'b'.repeat(64) + '"'), true);
  assert.throws(() => stampInspectorPin(source + '\n' + source, 'a'.repeat(64)));
  assert.throws(() => stampInspectorPin('', 'a'.repeat(64)));
  assert.throws(() => stampInspectorPin(source, 'not-a-digest'));
});
test('release tags cannot become shell arguments or stable-release claims', () => {
  assert.equal(releaseTag('client-v0.1.0-preview.1'), 'client-v0.1.0-preview.1');
  for (const invalid of ['main', 'v0.1.0', '--target', 'client-v0.1.0', 'client-v0.1.0-preview.1\n']) assert.throws(() => releaseTag(invalid));
});
