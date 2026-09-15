import assert from 'node:assert/strict';
import test from 'node:test';
import { POST } from '../app/api/product/route.ts';

function request(command) {
  const body = JSON.stringify({ schema_version: 'vibapp.web-product-request.experimental-v1', command, args: {} });
  return new Request('https://vibapp.example/api/product', {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'Content-Length': String(Buffer.byteLength(body)) }, body,
  });
}

test('disconnected website supports every boot-time settings read without contacting a backend', async () => {
  const originalFetch = globalThis.fetch;
  const originalLocal = process.env.VIBAPP_PREVIEW_LOCAL;
  process.env.VIBAPP_PREVIEW_LOCAL = '0';
  globalThis.fetch = () => { throw new Error('A disconnected read must not contact a backend'); };
  try {
    for (const command of ['get_model_settings', 'get_codeagent_settings', 'get_network_settings']) {
      const response = await POST(request(command));
      assert.equal(response.status, 200, command);
      const body = await response.json();
      assert.equal(body.ok, true);
      assert.equal(body.error, null);
      assert.equal(body.result.configured, false);
      assert.equal(body.result.readOnly, true);
      assert.equal(response.headers.get('cache-control'), 'no-store');
      if (command === 'get_model_settings') {
        assert.equal(body.result.generation.enabled, false);
        assert.equal(body.result.generation.baseUrl, '');
        assert.equal(body.result.generation.model, '');
        assert.equal(body.result.generation.hasApiKey, false);
        assert.equal(body.result.embedding.enabled, false);
        assert.equal(body.result.embedding.baseUrl, '');
      }
      if (command === 'get_codeagent_settings') assert.deepEqual(body.result.providers, []);
      if (command === 'get_network_settings') {
        assert.equal(body.result.p2pEnabled, false);
        assert.equal(body.result.rtcEnabled, false);
        assert.equal(body.result.hasTurnCredential, false);
        assert.deepEqual(body.result.turnUrls, []);
      }
    }
    for (const command of ['submit_need', 'complete_need', 'save_model_settings', 'save_codeagent_settings', 'save_network_settings', 'submit_development_task']) {
      const response = await POST(request(command));
      assert.equal(response.status, 403, command);
      assert.equal((await response.json()).ok, false);
    }
    const state = await (await POST(request('get_state'))).json();
    assert.equal(state.result.meta.mutation_authority, false);
    assert.deepEqual(state.result.jobs, []);
    assert.deepEqual(state.result.feed_errors, ['store-catalog-snapshot']);
    assert.ok(state.result.apps.length > 0);
    assert.ok(state.result.apps.every(app => app.publication_state === 'published'
      && app.install_eligible === false && app.launch_eligible === false));
  } finally {
    globalThis.fetch = originalFetch;
    if (originalLocal === undefined) delete process.env.VIBAPP_PREVIEW_LOCAL;
    else process.env.VIBAPP_PREVIEW_LOCAL = originalLocal;
  }
});
