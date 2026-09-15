import assert from 'node:assert/strict';
import { chmod, lstat, mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import {
  boundedEnvironment,
  createProductServer,
  disposeProductConfig,
  loadConfig,
  validateBridgeHealth,
} from '../server.mjs';

const EXPECTED_DIGEST = 'a'.repeat(64);
const BRIDGE_HEALTH = {
  status: 'ok',
  schema_version: 'vibapp.product-bridge.experimental-v1',
  service: 'vibapp-product-bridge',
  build_input_receipt: {
    schema_version: 'vibapp.desktop-build-input-receipt.experimental-v1',
    sha256: EXPECTED_DIGEST,
  },
  contracts: {
    cloud_codeagent_task: 'vibapp.cloud-codeagent-task.experimental-v3',
    cloud_codeagent_task_schema_sha256: 'fb2a49186349aa4542ab6bab9440fe2e5c48a9d7cc86085449a71e20ecdb73ab',
    codeagent_adapter: 'vibapp.codeagent-adapter.experimental-v1',
    codeagent_status: 'vibapp.codeagent-adapter-status.experimental-v2',
    codex_compatibility_policy: 'vibapp.codex-cli-compatibility.experimental-v1',
    delivery_worker: 'vibapp.desktop-delivery-worker.experimental-v2',
    provider_execution_identity: 'vibapp.provider-execution-identity.experimental-v1',
    source_handoff: 'vibapp.codeagent-source-handoff.experimental-v2',
  },
  pipeline_budget_seconds: 2700,
  bridge_lifetime_seconds: 2760,
  bridge_lifetime_margin_seconds: 60,
  worker_cleanup_budget_seconds: 20,
  lifecycle: 'cancel-join-confirm',
};

function bridgeResponse(result = BRIDGE_HEALTH) {
  return {
    schema_version: 'vibapp.product-bridge-response.experimental-v1',
    ok: true,
    result,
    error: null,
  };
}

async function writeFixtureBridge(root, implementation = 'fixture-A') {
  const bridge = join(root, 'fixture-bridge.mjs');
  await writeFile(bridge, `#!/usr/bin/env node
let raw = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => { raw += chunk; });
process.stdin.on('end', () => {
  const request = JSON.parse(raw);
  const health = ${JSON.stringify(BRIDGE_HEALTH)};
  process.stdout.write(JSON.stringify({
    schema_version: 'vibapp.product-bridge-response.experimental-v1',
    ok: true,
    result: request.command === 'health'
      ? health
      : { command: request.command, args: request.args, implementation: ${JSON.stringify(implementation)}, token_exposed: Boolean(process.env.VIBAPP_WEB_PRODUCT_TOKEN) },
    error: null,
  }) + '\\n');
  const holdOpenMs = Number(request.args?.hold_open_ms || 0);
  if (Number.isInteger(holdOpenMs) && holdOpenMs > 0 && holdOpenMs <= 5000) {
    setTimeout(() => {}, holdOpenMs);
  }
});
`, { mode: 0o700 });
  await chmod(bridge, 0o700);
  return bridge;
}

async function fixture() {
  const root = await mkdtemp(join(tmpdir(), 'vibapp-web-product-backend-'));
  const bridge = await writeFixtureBridge(root);
  const token = 'test-token-0123456789abcdef0123456789abcdef';
  const config = await loadConfig({
    VIBAPP_PRODUCT_BRIDGE_BIN: bridge,
    VIBAPP_WEB_PRODUCT_DATA_DIR: root,
    VIBAPP_WEB_PRODUCT_TOKEN: token,
    VIBAPP_WEB_PRODUCT_PORT: '31999',
    VIBAPP_EXPECTED_DESKTOP_BUILD_INPUTS_SHA256: EXPECTED_DIGEST,
  });
  config.port = 0;
  const server = createProductServer(config);
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const address = server.address();
  assert(address && typeof address === 'object');
  return {
    server,
    config,
    root,
    token,
    origin: `http://127.0.0.1:${address.port}`,
    closed: false,
  };
}

async function closeService(service) {
  if (service.closed) return;
  service.closed = true;
  await new Promise(resolve => service.server.close(resolve));
  await disposeProductConfig(service.config);
}

test('startup health binds the exact bridge build receipt and delivery contracts', async t => {
  assert.equal(validateBridgeHealth(bridgeResponse(), EXPECTED_DIGEST), BRIDGE_HEALTH);
  assert.throws(
    () => validateBridgeHealth(bridgeResponse(), 'b'.repeat(64)),
    /product-bridge-health-mismatch/,
  );
  const stale = structuredClone(bridgeResponse());
  delete stale.result.contracts;
  assert.throws(
    () => validateBridgeHealth(stale, EXPECTED_DIGEST),
    /product-bridge-health-mismatch/,
  );
  const missingStatus = structuredClone(bridgeResponse());
  delete missingStatus.result.status;
  assert.throws(
    () => validateBridgeHealth(missingStatus, EXPECTED_DIGEST),
    /product-bridge-health-mismatch/,
  );

  const root = await mkdtemp(join(tmpdir(), 'vibapp-web-product-config-'));
  const source = await writeFixtureBridge(root);
  const environment = {
    VIBAPP_PRODUCT_BRIDGE_BIN: source,
    VIBAPP_WEB_PRODUCT_DATA_DIR: root,
    VIBAPP_WEB_PRODUCT_TOKEN: 'config-token-0123456789abcdef0123456789abcdef',
    VIBAPP_WEB_PRODUCT_PORT: '31999',
    VIBAPP_EXPECTED_DESKTOP_BUILD_INPUTS_SHA256: EXPECTED_DIGEST,
  };
  const config = await loadConfig(environment);
  t.after(() => disposeProductConfig(config));
  assert.equal(config.bridgeHealth.build_input_receipt.sha256, EXPECTED_DIGEST);
  assert.match(config.bridgeSnapshot.sha256, /^[0-9a-f]{64}$/);
  assert.notEqual(config.bridgeSnapshot.path, source);
  assert.equal((await lstat(config.bridgeSnapshot.directory)).mode & 0o777, 0o700);
  assert.equal((await lstat(config.bridgeSnapshot.path)).mode & 0o777, 0o500);
  await assert.rejects(
    loadConfig({ ...environment, VIBAPP_EXPECTED_DESKTOP_BUILD_INPUTS_SHA256: 'b'.repeat(64) }),
    /product-bridge-health-mismatch/,
  );
});

function invoke(origin, token, body, headers = {}) {
  const encoded = JSON.stringify(body);
  return fetch(`${origin}/v1/invoke`, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'content-length': String(Buffer.byteLength(encoded)),
      authorization: `Bearer ${token}`,
      ...headers,
    },
    body: encoded,
  });
}

test('authenticated allowlisted invocation reaches the bridge without exposing the server token', async t => {
  const service = await fixture();
  t.after(() => closeService(service));
  const response = await invoke(service.origin, service.token, {
    schema_version: 'vibapp.web-product-request.experimental-v1',
    command: 'get_state',
    args: {},
  });
  assert.equal(response.status, 200);
  const value = await response.json();
  assert.equal(value.ok, true);
  assert.equal(value.result.command, 'get_state');
  assert.equal(value.result.token_exposed, false);
  assert.equal(JSON.stringify(value).includes(service.token), false);

  const networkSettings = await invoke(service.origin, service.token, {
    schema_version: 'vibapp.web-product-request.experimental-v1',
    command: 'get_network_settings',
    args: {},
  });
  assert.equal(networkSettings.status, 200);
  const networkValue = await networkSettings.json();
  assert.equal(networkValue.result.command, 'get_network_settings');
  assert.equal(networkValue.result.token_exposed, false);
});

test('missing authorization and browser-origin requests fail before bridge execution', async t => {
  const service = await fixture();
  t.after(() => closeService(service));
  const body = {
    schema_version: 'vibapp.web-product-request.experimental-v1',
    command: 'health',
    args: {},
  };
  const denied = await invoke(service.origin, 'wrong-token-0123456789abcdef0123456789abcdef', body);
  assert.equal(denied.status, 403);
  const browserDirect = await invoke(service.origin, service.token, body, { origin: 'https://attacker.invalid' });
  assert.equal(browserDirect.status, 403);
});

test('unknown commands and non-exact request shapes are rejected', async t => {
  const service = await fixture();
  t.after(() => closeService(service));
  const unknown = await invoke(service.origin, service.token, {
    schema_version: 'vibapp.web-product-request.experimental-v1',
    command: 'shell',
    args: {},
  });
  assert.equal(unknown.status, 400);
  const extra = await invoke(service.origin, service.token, {
    schema_version: 'vibapp.web-product-request.experimental-v1',
    command: 'health',
    args: {},
    token: service.token,
  });
  assert.equal(extra.status, 400);
});

test('bounded bridge environment forwards only the public Registry path, never server authority', () => {
  const environment = boundedEnvironment({
    HOME: '/tmp/empty-home',
    PATH: '/usr/bin:/bin',
    VIBAPP_PUBLIC_REGISTRY_ROOT: '/tmp/public-registry/data',
    VIBAPP_WEB_PRODUCT_TOKEN: 'must-not-cross-the-bridge',
    VIBAPP_ROOMHASH_NODE_BIN: '/tmp/untrusted-node',
  });
  assert.equal(environment.VIBAPP_PUBLIC_REGISTRY_ROOT, '/tmp/public-registry/data');
  assert.equal(environment.VIBAPP_WEB_PRODUCT_TOKEN, undefined);
  assert.equal(environment.VIBAPP_ROOMHASH_NODE_BIN, undefined);
  assert.throws(
    () => boundedEnvironment({ VIBAPP_PUBLIC_REGISTRY_ROOT: 'relative/data' }),
    /bounded absolute path/,
  );
});

test('bridge admission slots remain occupied until each child exits', async t => {
  const service = await fixture();
  t.after(() => closeService(service));
  const body = {
    schema_version: 'vibapp.web-product-request.experimental-v1',
    command: 'get_state',
    args: { hold_open_ms: 600 },
  };
  const admitted = await Promise.all(Array.from({ length: 4 }, () => invoke(
    service.origin,
    service.token,
    body,
  )));
  assert.ok(admitted.every(response => response.status === 200));

  const busy = await invoke(service.origin, service.token, {
    schema_version: 'vibapp.web-product-request.experimental-v1',
    command: 'get_state',
    args: {},
  });
  assert.equal(busy.status, 503);
  assert.equal((await busy.json()).error, 'busy');

  await new Promise(resolve => setTimeout(resolve, 750));
  const recovered = await invoke(service.origin, service.token, {
    schema_version: 'vibapp.web-product-request.experimental-v1',
    command: 'get_state',
    args: {},
  });
  assert.equal(recovered.status, 200);
});

test('a child error while its process is alive does not release its admission slot', async t => {
  const service = await fixture();
  t.after(() => closeService(service));
  const body = {
    schema_version: 'vibapp.web-product-request.experimental-v1',
    command: 'get_state',
    args: { hold_open_ms: 700 },
  };
  const admitted = await Promise.all(Array.from({ length: 4 }, () => invoke(
    service.origin,
    service.token,
    body,
  )));
  assert.ok(admitted.every(response => response.status === 200));
  assert.equal(service.config.children.size, 4);
  assert.equal(service.config.slots.size, 4);

  for (const child of service.config.children) {
    assert.notEqual(child.pid, undefined);
    child.emit('error', new Error('synthetic-live-child-error'));
  }
  assert.equal(service.config.children.size, 4);
  assert.equal(service.config.slots.size, 4);

  const busy = await invoke(service.origin, service.token, {
    schema_version: 'vibapp.web-product-request.experimental-v1',
    command: 'get_state',
    args: {},
  });
  assert.equal(busy.status, 503);
  assert.equal((await busy.json()).error, 'busy');

  await new Promise(resolve => setTimeout(resolve, 850));
  const recovered = await invoke(service.origin, service.token, {
    schema_version: 'vibapp.web-product-request.experimental-v1',
    command: 'get_state',
    args: {},
  });
  assert.equal(recovered.status, 200);
});

test('replacing the configured bridge path cannot change the startup-bound executable', async () => {
  const service = await fixture();
  const snapshotPath = service.config.bridgeSnapshot.path;
  const snapshotDirectory = service.config.bridgeSnapshot.directory;
  await writeFixtureBridge(service.root, 'fixture-B');

  const response = await invoke(service.origin, service.token, {
    schema_version: 'vibapp.web-product-request.experimental-v1',
    command: 'get_state',
    args: {},
  });
  assert.equal(response.status, 200);
  const value = await response.json();
  assert.equal(value.result.implementation, 'fixture-A');

  const health = await fetch(`${service.origin}/healthz`);
  assert.equal(health.status, 200);
  const healthValue = await health.json();
  assert.equal(healthValue.bridge.executable_sha256, service.config.bridgeSnapshot.sha256);

  await closeService(service);
  await assert.rejects(lstat(snapshotPath), { code: 'ENOENT' });
  await assert.rejects(lstat(snapshotDirectory), { code: 'ENOENT' });
});

test('mutating the private bridge snapshot fails health and invocation closed', async t => {
  const service = await fixture();
  t.after(() => closeService(service));
  await chmod(service.config.bridgeSnapshot.path, 0o700);
  await writeFile(
    service.config.bridgeSnapshot.path,
    '#!/usr/bin/env node\nprocess.stdout.write("tampered\\n");\n',
  );

  const health = await fetch(`${service.origin}/healthz`);
  assert.equal(health.status, 503);
  assert.equal((await health.json()).error, 'bridge-unavailable');

  const response = await invoke(service.origin, service.token, {
    schema_version: 'vibapp.web-product-request.experimental-v1',
    command: 'get_state',
    args: {},
  });
  assert.equal(response.status, 503);
  assert.equal((await response.json()).error, 'bridge-unavailable');
  assert.equal(service.config.children.size, 0);
  assert.equal(service.config.slots.size, 0);
});
