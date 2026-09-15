import test from 'node:test';
import assert from 'node:assert/strict';
import { ForegroundClient, bindForegroundWorker, foregroundBinding, foregroundFields } from '../foreground-session.mjs';

const request = { package_digest_sha256: 'a'.repeat(64), component_sha256: 'b'.repeat(64), session: 'web-session-test' };
const binding = foregroundBinding(request, { surface: 'surface-main', route: 'home' });

test('field transport matches native empty/false semantics without blocking unrelated buttons', () => {
  const fields = [{ field: 'name', kind: 'text', required: true }, { field: 'flag', kind: 'boolean' }];
  assert.deepEqual(foregroundFields(fields, [{ field: 'name', value: { tag: 'text', value: '' } }, { field: 'flag', value: { tag: 'boolean', value: false } }]),
    [{ field: 'name', value: { tag: 'text', val: '' } }, { field: 'flag', value: { tag: 'boolean', val: false } }]);
  assert.deepEqual(foregroundFields(fields, []), []);
  assert.throws(() => foregroundFields(fields, [{ field: 'missing', value: { tag: 'text', value: '' } }]), /field-invalid/);
});

test('foreground date/time/decimal values are validated before guest execution', () => {
  for (const [kind, value] of [['date', { year: 2025, month: 2, day: 29 }], ['time', { hour: 24, minute: 0, second: 0 }], ['decimal', 'NaN']]) {
    assert.throws(() => foregroundFields([{ field: 'value', kind }], [{ field: 'value', value: { tag: kind, value } }]), /invalid/);
  }
  assert.equal(foregroundFields([{ field: 'value', kind: 'integer' }], [{ field: 'value', value: { tag: 'integer', value: 0 } }])[0].value.val, 0n);
});
const payload = (id = 'event-1') => ({ appId: 'ai.vibapp.test', entrypoint: 'main', packageDigestSha256: binding.package_digest_sha256,
  componentSha256: binding.component_sha256, generation: binding.generation, session: binding.session, surface: binding.surface,
  route: binding.route, eventId: id });

function pair() {
  const left = { closed: false, onmessage: null, close() { this.closed = true; } };
  const right = { closed: false, onmessage: null, close() { this.closed = true; } };
  left.postMessage = data => queueMicrotask(() => { if (!right.closed) right.onmessage?.({ data }); });
  right.postMessage = data => queueMicrotask(() => { if (!left.closed) left.onmessage?.({ data }); });
  return { left, right };
}

test('one worker keeps foreground state across refresh and actions until close', async () => {
  const { left, right } = pair(); let state = 0; let terminated = 0;
  bindForegroundWorker(right, binding, message => { if (message.operation === 'action') state++; return { state }; }, () => {});
  const client = new ForegroundClient({ port: left, worker: { terminate() { terminated++; } }, binding, appId: 'ai.vibapp.test' });
  assert.deepEqual(await client.call('refresh', payload()), { state: 0 });
  assert.deepEqual(await client.call('action', { ...payload('event-2'), action: 'increment' }), { state: 1 });
  assert.deepEqual(await client.call('refresh', payload('event-3')), { state: 1 });
  assert.equal(terminated, 0);
  client.close(); client.close();
  assert.equal(terminated, 1);
  await assert.rejects(client.call('refresh', payload('event-4')), /unavailable/);
});

test('worker rejects replay, wrong session and extra authority before guest execution', () => {
  for (const mutate of [message => { message.sequence = 2; }, message => { message.binding.session = 'other'; }, message => { message.install = true; }]) {
    const port = { close() {}, postMessage() {} }; let calls = 0; let closed = 0;
    bindForegroundWorker(port, binding, () => { calls++; }, () => { closed++; });
    const message = { schema_version: 'vibapp.foreground-session.experimental-v1', binding: { ...binding }, sequence: 1,
      event_id: 'event-1', operation: 'action', action: 'test', fields: [] };
    mutate(message); port.onmessage({ data: message });
    assert.equal(calls, 0); assert.equal(closed, 1);
  }
});

test('timed out guest terminates the worker and rejects pending interaction', async () => {
  let terminated = 0;
  const client = new ForegroundClient({ port: { close() {}, postMessage() {} }, worker: { terminate() { terminated++; } }, binding, appId: 'ai.vibapp.test', timeoutMs: 10 });
  await assert.rejects(client.call('refresh', payload()), /timeout/);
  assert.equal(terminated, 1);
  assert.equal(client.pending, null);
});

test('forged response closes session and cannot update the UI', async () => {
  const port = { close() {}, postMessage() {} }; let terminated = 0;
  const client = new ForegroundClient({ port, worker: { terminate() { terminated++; } }, binding, appId: 'ai.vibapp.test' });
  const result = client.call('refresh', payload());
  port.onmessage({ data: { schema_version: 'vibapp.foreground-session.experimental-v1', sequence: 1, event_id: 'event-1', binding: { ...binding, generation: 'other' }, result: {}, error: null } });
  await assert.rejects(result, /binding-mismatch/);
  assert.equal(terminated, 1);
});

test('concurrent actions and stale UI bindings are rejected before posting', async () => {
  const client = new ForegroundClient({ port: { close() {}, postMessage() {} }, worker: { terminate() {} }, binding, appId: 'ai.vibapp.test' });
  await assert.rejects(client.call('action', { ...payload(), appId: 'ai.vibapp.wrong' }), /stale-binding/);
  await assert.rejects(client.call('action', { ...payload(), session: 'stale' }), /stale-binding/);
  const first = client.call('refresh', payload());
  await assert.rejects(client.call('action', payload('event-2')), /unavailable/);
  client.close(); await assert.rejects(first, /closed/);
});

test('oversized local calls do not consume the sequence; post failures clean up immediately', async () => {
  const { left, right } = pair();
  bindForegroundWorker(right, binding, () => true, () => {});
  const client = new ForegroundClient({ port: left, worker: { terminate() {} }, binding, appId: 'ai.vibapp.test' });
  await assert.rejects(client.call('action', { ...payload(), action: 'x'.repeat(128 * 1024) }), /message-limit/);
  assert.equal(await client.call('refresh', payload()), true);
  client.port.postMessage = () => { throw new Error('closed-port'); };
  await assert.rejects(client.call('refresh', payload('event-2')), /closed-port/);
  assert.equal(client.closed, true);
  assert.equal(client.pending, null);
});
