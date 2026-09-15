import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { access, chmod, mkdtemp, readFile, writeFile } from 'node:fs/promises';
import net from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import {
  LOCAL_PREVIEW_ORIGIN as localPreviewOrigin,
  PRODUCTION_PREVIEW_ORIGIN as productionPreviewOrigin,
} from '../scripts/preview-config.mjs';
import { computeDesktopBuildInputs } from '../../../desktop/scripts/desktop-build-inputs.mjs';

const websiteRoot = fileURLToPath(new URL('../', import.meta.url));
const startHelper = fileURLToPath(new URL('../scripts/start-local.mjs', import.meta.url));
const localBuildHelper = fileURLToPath(new URL('../scripts/build-local.mjs', import.meta.url));
const productionStartHelper = fileURLToPath(new URL('../scripts/start-production.mjs', import.meta.url));
const productionBuildHelper = fileURLToPath(new URL('../scripts/build-production.mjs', import.meta.url));
const syncWebGui = fileURLToPath(new URL('../../../web-client-core/sync-web-gui.mjs', import.meta.url));
const registrySnapshotPath = fileURLToPath(new URL('../public/data/registry.snapshot.json', import.meta.url));
const stableProductionDataRoot = fileURLToPath(new URL('../../registry/generated/production-public-data/', import.meta.url));
const privateComponentsPath = fileURLToPath(new URL(
  '../public/launcher/components/d3844f16cdfee3634a3652d6f5e43d54adad18c198cf3b6837a6d5d149e661aa',
  import.meta.url,
));
const localEnvironment = {
  ...process.env,
  NODE_ENV: 'production',
  VIBAPP_PREVIEW_LOCAL: '1',
  VIBAPP_PREVIEW_ORIGIN: localPreviewOrigin,
};

async function compatibleBridgeFixture() {
  const root = await mkdtemp(join(tmpdir(), 'vibapp-website-bridge-'));
  const bridge = join(root, 'fixture-bridge.mjs');
  const receipt = await computeDesktopBuildInputs();
  const health = {
    status: 'ok',
    schema_version: 'vibapp.product-bridge.experimental-v1',
    service: 'vibapp-product-bridge',
    build_input_receipt: {
      schema_version: 'vibapp.desktop-build-input-receipt.experimental-v1',
      sha256: receipt.sha256,
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
  await writeFile(bridge, `#!/usr/bin/env node
let raw = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => { raw += chunk; });
process.stdin.on('end', () => {
  const request = JSON.parse(raw);
  const health = ${JSON.stringify(health)};
  process.stdout.write(JSON.stringify({
    schema_version: 'vibapp.product-bridge-response.experimental-v1',
    ok: request.command === 'health',
    result: request.command === 'health' ? health : null,
    error: request.command === 'health' ? null : 'fixture-only-health',
  }) + '\\n');
});
`, { mode: 0o700 });
  await chmod(bridge, 0o700);
  return { bridge, dataDir: join(root, 'data'), receipt };
}

function unusedLoopbackPort() {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.once('error', reject);
    probe.listen(0, '127.0.0.1', () => {
      const address = probe.address();
      assert.ok(address && typeof address === 'object');
      const { port } = address;
      probe.close(error => error ? reject(error) : resolve(port));
    });
  });
}

async function waitForResponse(url, child, logs) {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`Website exited before becoming ready (${child.exitCode}).\n${logs()}`);
    }
    try {
      const response = await fetch(url);
      if (response.ok) return response;
    } catch {
      // The bounded retry only covers the process startup window.
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(`Website did not become ready within 30 seconds.\n${logs()}`);
}

function productRequest(origin, command, args = {}) {
  const body = JSON.stringify({
    schema_version: 'vibapp.web-product-request.experimental-v1',
    command,
    args,
  });
  return fetch(`${origin}/api/product`, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'content-length': String(Buffer.byteLength(body)),
    },
    body,
  });
}

async function stopChild(child) {
  if (child.exitCode !== null) return;
  child.kill('SIGTERM');
  await Promise.race([
    new Promise(resolve => child.once('exit', resolve)),
    new Promise(resolve => setTimeout(resolve, 5_000)),
  ]);
  if (child.exitCode === null) child.kill('SIGKILL');
}

function syncProjection(local) {
  const environment = local ? {
    ...process.env,
    VIBAPP_WEB_PRIVATE_PREVIEW_MODE: 'loopback-local-development',
    VIBAPP_PREVIEW_LOCAL: '1',
    VIBAPP_PREVIEW_ORIGIN: localPreviewOrigin,
  } : {
    ...process.env,
    npm_lifecycle_event: 'prebuild',
    VIBAPP_PREVIEW_LOCAL: '0',
    VIBAPP_PREVIEW_ORIGIN: productionPreviewOrigin,
  };
  const result = spawnSync(process.execPath, [syncWebGui], {
    cwd: websiteRoot,
    env: environment,
    encoding: 'utf8',
    maxBuffer: 10 * 1024 * 1024,
    timeout: 120_000,
  });
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
}

test('start:local fixes the preview origin and keeps the Website loopback-only', () => {
  const result = spawnSync(process.execPath, [startHelper, '--print-config'], {
    cwd: websiteRoot,
    env: { ...process.env, VIBAPP_PRODUCT_BRIDGE_BIN: '' },
    encoding: 'utf8',
  });
  assert.equal(result.status, 0, result.stderr);
  const config = JSON.parse(result.stdout);
  assert.equal(config.environment.NODE_ENV, 'production');
  assert.equal(config.environment.VIBAPP_PREVIEW_LOCAL, '1');
  assert.equal(config.environment.VIBAPP_PREVIEW_ORIGIN, localPreviewOrigin);
  assert.deepEqual(config.command.slice(-3), ['start', '--hostname', '127.0.0.1']);
  assert.equal(config.productBackend.bridge, fileURLToPath(new URL('../../../desktop/target/release/vibapp-product-bridge', import.meta.url)));
});

test('ordinary start forces the production preview origin', () => {
  const result = spawnSync(process.execPath, [productionStartHelper, '--print-config'], {
    cwd: websiteRoot,
    env: {
      ...process.env,
      VIBAPP_PREVIEW_LOCAL: '1',
      VIBAPP_PREVIEW_ORIGIN: localPreviewOrigin,
    },
    encoding: 'utf8',
  });
  assert.equal(result.status, 0, result.stderr);
  const config = JSON.parse(result.stdout);
  assert.equal(config.environment.NODE_ENV, 'production');
  assert.equal(config.environment.VIBAPP_PREVIEW_LOCAL, '0');
  assert.equal(config.environment.VIBAPP_PREVIEW_ORIGIN, productionPreviewOrigin);
});

test('server-rendered preview origin exactly matches the CSP frame-src', { timeout: 240_000 }, async () => {
  syncProjection(true);
  const localSnapshot = JSON.parse(await readFile(registrySnapshotPath, 'utf8'));
  assert.equal(localSnapshot.consumer_notes.website_projection_mode, 'loopback-local-development');
  assert.equal(localSnapshot.consumer_notes.local_private_preview_enabled, true);
  assert.equal(localSnapshot.browser_preview_records.length, 1);
  assert.equal(localSnapshot.browser_preview_records[0].publication.state, 'private-candidate');
  const stableSnapshotBytes = await readFile(join(stableProductionDataRoot, 'registry.snapshot.json'));
  const stableSnapshot = JSON.parse(stableSnapshotBytes);
  const stableIndex = JSON.parse(await readFile(
    join(stableProductionDataRoot, 'package-locators', 'index.json'),
    'utf8',
  ));
  assert.equal(stableSnapshot.consumer_notes.website_projection_mode, 'production-public-only');
  assert.equal(stableSnapshot.consumer_notes.local_private_preview_enabled, false);
  assert.deepEqual(stableSnapshot.browser_preview_records, []);
  assert.equal(
    stableIndex.registry_snapshot_sha256,
    createHash('sha256').update(stableSnapshotBytes).digest('hex'),
  );

  const build = spawnSync(process.execPath, [localBuildHelper], {
    cwd: websiteRoot,
    env: localEnvironment,
    encoding: 'utf8',
    maxBuffer: 10 * 1024 * 1024,
    timeout: 90_000,
  });
  assert.equal(build.status, 0, `${build.stdout}\n${build.stderr}`);

  const port = await unusedLoopbackPort();
  const productPort = await unusedLoopbackPort();
  const bridgeFixture = await compatibleBridgeFixture();
  const child = spawn(process.execPath, [startHelper, '--port', String(port)], {
    cwd: websiteRoot,
    env: {
      ...localEnvironment,
      VIBAPP_WEB_PRODUCT_PORT: String(productPort),
      VIBAPP_PRODUCT_BRIDGE_BIN: bridgeFixture.bridge,
      VIBAPP_WEB_PRODUCT_DATA_DIR: bridgeFixture.dataDir,
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let output = '';
  const append = chunk => { output = (output + chunk).slice(-64 * 1024); };
  child.stdout.on('data', append);
  child.stderr.on('data', append);

  try {
    const response = await waitForResponse(`http://127.0.0.1:${port}/`, child, () => output);
    const csp = response.headers.get('content-security-policy');
    assert.ok(csp, 'Content-Security-Policy header is missing');
    const frameDirective = csp.split(';').map(value => value.trim()).find(value => value.startsWith('frame-src '));
    assert.equal(frameDirective, `frame-src ${localPreviewOrigin}`);
    const connectDirective = csp.split(';').map(value => value.trim()).find(value => value.startsWith('connect-src '));
    assert.equal(connectDirective, "connect-src 'self' wss://tracker.webtorrent.dev wss://tracker.openwebtorrent.com wss://tracker.btorrent.xyz");
    const html = await response.text();
    assert.ok(html.includes(localPreviewOrigin), 'server-rendered preview origin is missing from HTML');
    const localShare = await fetch(`http://127.0.0.1:${port}/apps/ai.vibapp.hello`);
    assert.equal(localShare.status, 200, 'explicit loopback build should retain the private local acceptance route');
  } finally {
    await stopChild(child);
  }
});

test('ordinary production start keeps server rendering and CSP on the public preview origin', { timeout: 240_000 }, async () => {
  syncProjection(false);
  const productionSnapshot = JSON.parse(await readFile(registrySnapshotPath, 'utf8'));
  assert.equal(productionSnapshot.consumer_notes.website_projection_mode, 'production-public-only');
  assert.equal(productionSnapshot.consumer_notes.local_private_preview_enabled, false);
  assert.deepEqual(productionSnapshot.browser_preview_records, []);
  assert.ok(productionSnapshot.records.every(record => (
    record.publication?.state === 'published'
    && record.verification?.status === 'verified'
    && record.verification?.revocation === 'not-revoked'
    && record.source?.visibility === 'public'
  )));
  assert.deepEqual(
    await readFile(registrySnapshotPath),
    await readFile(join(stableProductionDataRoot, 'registry.snapshot.json')),
  );
  await assert.rejects(access(privateComponentsPath));

  const build = spawnSync(process.execPath, [productionBuildHelper], {
    cwd: websiteRoot,
    env: localEnvironment,
    encoding: 'utf8',
    maxBuffer: 10 * 1024 * 1024,
    timeout: 90_000,
  });
  assert.equal(build.status, 0, `${build.stdout}\n${build.stderr}`);

  const port = await unusedLoopbackPort();
  const child = spawn(process.execPath, [productionStartHelper, '--hostname', '127.0.0.1', '--port', String(port)], {
    cwd: websiteRoot,
    env: localEnvironment,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let output = '';
  const append = chunk => { output = (output + chunk).slice(-64 * 1024); };
  child.stdout.on('data', append);
  child.stderr.on('data', append);

  try {
    const response = await waitForResponse(`http://127.0.0.1:${port}/`, child, () => output);
    const csp = response.headers.get('content-security-policy');
    assert.ok(csp, 'Content-Security-Policy header is missing');
    const frameDirective = csp.split(';').map(value => value.trim()).find(value => value.startsWith('frame-src '));
    assert.equal(frameDirective, `frame-src ${productionPreviewOrigin}`);
    const connectDirective = csp.split(';').map(value => value.trim()).find(value => value.startsWith('connect-src '));
    assert.equal(connectDirective, "connect-src 'self' wss://tracker.webtorrent.dev wss://tracker.openwebtorrent.com wss://tracker.btorrent.xyz");
    const html = await response.text();
    assert.ok(html.includes(productionPreviewOrigin), 'production preview origin is missing from HTML');
    assert.ok(!html.includes(localPreviewOrigin), 'local preview origin leaked into production HTML');
    const privateShare = await fetch(`http://127.0.0.1:${port}/apps/ai.vibapp.hello`);
    assert.equal(privateShare.status, 404, 'private local candidate leaked into a production share route');
    const syntheticBindingShare = await fetch(`http://127.0.0.1:${port}/apps/ai.vibapp.fixture.weather-preview`);
    assert.equal(syntheticBindingShare.status, 404, 'a synthetic metadata-only binding became a public runtime route');
    const publicOrigin = `http://127.0.0.1:${port}`;
    const publicState = await productRequest(publicOrigin, 'get_state');
    assert.equal(publicState.status, 200);
    const publicStateValue = await publicState.json();
    assert.equal(publicStateValue.result.meta.runtime_mode, 'public-website-read-only');
    assert.equal(publicStateValue.result.meta.mutation_authority, false);
    assert.deepEqual(publicStateValue.result.jobs, []);
    assert.deepEqual(publicStateValue.result.apps, []);
    const deniedMutation = await productRequest(publicOrigin, 'submit_need', {
      title: 'must not cross the public route',
      description: 'an unauthenticated public visitor has no shared mutation authority',
      embedding_consent: false,
    });
    assert.equal(deniedMutation.status, 403);
    assert.equal((await deniedMutation.json()).error, 'authenticated-user-session-required');
  } finally {
    await stopChild(child);
  }
});

test('local start fails closed when the existing build has production CSP', () => {
  const port = 31_337;
  const result = spawnSync(process.execPath, [startHelper, '--port', String(port)], {
    cwd: websiteRoot,
    encoding: 'utf8',
  });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /build preview origin does not match startup policy/);
});
