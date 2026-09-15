import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

function fixture() {
  const source = readFileSync(new URL('../web-bridge.js', import.meta.url), 'utf8');
  const queue = source.slice(source.indexOf('let parentProductQueueTail ='), source.indexOf('\nconst storageReady ='));
  const requests = new Map(), timers = new Map(), sent = [];
  let now = 0, id = 0, failPost = false;
  const context = vm.createContext({ Promise, Error, Date: { now: () => now },
    crypto: { randomUUID: () => String(++id) }, parentStorageBinding: Promise.resolve(),
    parentProductRequests: requests,
    parentStoragePort: { postMessage(message) { if (failPost) throw new Error('port-closed'); sent.push(message); } },
    setTimeout(fn, delay) { const key = ++id; timers.set(key, { fn, delay }); return key; },
    clearTimeout(key) { timers.delete(key); },
  });
  vm.runInContext(queue, context);
  return { sent, requests, timers, call: (...args) => context.parentProduct(...args),
    advance: value => { now = value; }, failPost: value => { failPost = value; },
    settle(error = null) {
      const [key, pending] = requests.entries().next().value;
      requests.delete(key); timers.delete(pending.timeout);
      if (error) pending.reject(error); else pending.resolve({ ok: true });
    } };
}
const flush = () => new Promise(resolve => setImmediate(resolve));

test('six concurrent startup reads dispatch once each without exceeding one local request', async () => {
  const f = fixture();
  const calls = Array.from({ length: 6 }, (_, i) => f.call('settings-' + i));
  for (let i = 0; i < 6; i++) {
    await flush();
    assert.equal(f.sent.length, i + 1);
    assert.equal(f.requests.size, 1);
    f.settle();
  }
  await Promise.all(calls);
  assert.equal(f.timers.size, 0);
});

test('a failed mutation is not replayed and does not poison following reads', async () => {
  const f = fixture();
  const mutation = assert.rejects(f.call('submit_development_task'), /busy/);
  const read = f.call('get_state');
  await flush(); f.settle(new Error('busy')); await mutation;
  await flush(); assert.deepEqual(f.sent.map(value => value.command), ['submit_development_task', 'get_state']);
  f.settle(); await read;
});

test('queue is bounded and expiration includes time spent waiting before dispatch', async () => {
  const f = fixture();
  const first = f.call('get_state');
  const waiting = Array.from({ length: 15 }, () => assert.rejects(f.call('settings'), /超时/));
  await assert.rejects(f.call('overflow'), /队列已满/);
  await flush(); f.advance(80_001); f.settle(); await first;
  await Promise.all(waiting);
  assert.equal(f.sent.length, 1);
  const recovered = f.call('get_state'); await flush(); f.settle(); await recovered;
  assert.equal(f.sent.length, 2);
});

test('post failure releases timer and pending entry, with no dispatched retry', async () => {
  const f = fixture(); f.failPost(true);
  await assert.rejects(f.call('save_model_settings'), /port-closed/);
  assert.equal(f.requests.size, 0); assert.equal(f.timers.size, 0);
  f.failPost(false);
  const read = f.call('get_state'); await flush(); f.settle(); await read;
  assert.equal(f.sent.length, 1);
});
