import test from 'node:test';
import assert from 'node:assert/strict';
import { mobileProduct, validateProductRequest, validateStorageRequest } from '../host/mobile-protocol.mjs';

test('mobile boot exposes disconnected services, not fake native authority', () => {
  assert.equal(mobileProduct('get_state').meta.local_codeagent_connected, false);
  assert.equal(mobileProduct('get_model_settings').generation.enabled, false);
  assert.equal(mobileProduct('get_network_status').foregroundOnly, true);
  assert.throws(() => mobileProduct('submit_development_task'), /not connected/);
  assert.throws(() => mobileProduct('install'), /not connected/);
});
test('storage capability admits only bounded application keys', () => {
  const request = { schema_version: 'vibapp.web-parent-storage.experimental-v1', kind: 'storage-get', request_id: 'id', key: 'vibapp.ui_locale', value: null };
  assert.equal(validateStorageRequest(request), true);
  assert.equal(validateStorageRequest({ ...request, key: 'secret' }), false);
  assert.equal(validateStorageRequest({ ...request, unexpected: 1 }), false);
  assert.equal(validateStorageRequest({ ...request, kind: 'storage-set', value: 'x'.repeat(1024 * 1024 + 1) }), false);
});
test('product envelope rejects extra authority and malformed arguments', () => {
  const request = { schema_version: 'vibapp.web-parent-product.experimental-v1', kind: 'product-invoke', request_id: 'id', command: 'get_state', args: {} };
  assert.equal(validateProductRequest(request), true);
  assert.equal(validateProductRequest({ ...request, args: [] }), false);
  assert.equal(validateProductRequest({ ...request, token: 'untrusted' }), false);
});
