import assert from 'node:assert/strict';
import { createServer, request as httpRequest } from 'node:http';
import { appendFile, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import {
  DurableInvalidBootstrapAudit,
  canonicalAuditJson,
} from '../invalid-bootstrap-audit.mjs';
import {
  classifyParentBindingFailure,
  previewBoundary,
  validateGuiSyncManifest,
  validateParentBinding,
} from '../preview-protocol.mjs';
import { createPreviewServer, loadConfig } from '../server.mjs';

function listen(server, host = '127.0.0.1') {
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, host, () => {
      server.off('error', reject);
      resolve(server.address().port);
    });
  });
}

function close(server) {
  return new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
}

function rawRequest({ port, path = '/', headers = {} }) {
  return new Promise((resolve, reject) => {
    const req = httpRequest({ host: '127.0.0.1', port, path, headers }, response => {
      const chunks = [];
      response.on('data', chunk => chunks.push(chunk));
      response.on('end', () => resolve({
        status: response.statusCode,
        headers: response.headers,
        body: Buffer.concat(chunks),
      }));
    });
    req.on('error', reject);
    req.end();
  });
}

function parentDocument(previewOrigin) {
  return `<!doctype html>
<iframe id="preview" credentialless sandbox="allow-scripts allow-same-origin allow-forms"></iframe>
<script type="module">
const previewOrigin = ${JSON.stringify(previewOrigin)};
const nonce = 'preview-frame-nonce-0123456789abcdef';
const frame = document.querySelector('#preview');
frame.src = previewOrigin + '/preview.html#nonce=' + encodeURIComponent(nonce);
frame.addEventListener('load', () => {
  const channel = new MessageChannel();
  frame.contentWindow.postMessage({
    schema_version: 'vibapp.preview-frame-bind.experimental-v1',
    kind: 'bind-preview-frame',
    channel_id: 'preview-frame-channel-0123456789abcdef',
    nonce,
    preview_origin: previewOrigin,
  }, previewOrigin, [channel.port2]);
}, { once: true });
</script>`;
}

test('preview launcher cache token is bound to the exact shared GUI manifest', async () => {
  const manifest = JSON.parse(await readFile(new URL('../../product-platform/website/public/launcher/gui-sync-manifest.json', import.meta.url)));
  const cacheVersion = validateGuiSyncManifest(manifest);
  const broker = await readFile(new URL('../preview-broker.mjs', import.meta.url), 'utf8');
  assert.match(broker, /documentRoot\.dataset\.guiCacheVersion/);
  assert.match(broker, /launcherUrl\.searchParams\.set\('v', guiCacheVersion\)/);
  assert.match(broker, /vibapp\.web-storage-bind\.experimental-v1/);
  assert.match(broker, /\[port\]/);
  assert.doesNotMatch(broker, /index\.html\?v=[a-z0-9-]+/i);
  assert.throws(
    () => validateGuiSyncManifest({ ...manifest, adapter: { ...manifest.adapter, cache_version: '20260827-6' } }),
    /gui-manifest-cache-version/,
  );
  assert.throws(
    () => validateGuiSyncManifest({ ...manifest, files: { ...manifest.files, 'app.js': { ...manifest.files['app.js'], exact_match: false } } }),
    /gui-manifest-app-drift/,
  );
});

test('two explicit localhost origins expose a credentialless strict-origin parent and cookie-rejecting preview host', async () => {
  const placeholderParent = createServer();
  const parentPort = await listen(placeholderParent);
  await close(placeholderParent);
  const placeholderPreview = createServer();
  const previewPort = await listen(placeholderPreview);
  await close(placeholderPreview);
  const parentOrigin = `http://127.0.0.1:${parentPort}`;
  const previewOrigin = `http://127.0.0.1:${previewPort}`;
  const auditRoot = await mkdtemp(join(tmpdir(), 'vibapp-preview-audit-test-'));
  assert.notEqual(parentOrigin, previewOrigin);

  const parent = createServer((request, response) => {
    const body = Buffer.from(parentDocument(previewOrigin));
    response.writeHead(200, {
      'Content-Type': 'text/html; charset=utf-8',
      'Content-Length': String(body.byteLength),
      'Content-Security-Policy': `default-src 'none'; script-src 'unsafe-inline'; frame-src ${previewOrigin}`,
    });
    response.end(body);
  });
  const config = loadConfig({
    NODE_ENV: 'test',
    VIBAPP_PREVIEW_ORIGIN: previewOrigin,
    VIBAPP_PARENT_ORIGIN: parentOrigin,
    VIBAPP_PREVIEW_PORT: String(previewPort),
    VIBAPP_PREVIEW_AUDIT_PATH: join(auditRoot, 'invalid-bootstrap-audit.jsonl'),
  });
  const preview = createPreviewServer(config);
  await new Promise((resolve, reject) => parent.listen(parentPort, '127.0.0.1').once('listening', resolve).once('error', reject));
  await new Promise((resolve, reject) => preview.listen(previewPort, '127.0.0.1').once('listening', resolve).once('error', reject));

  try {
    const parentResponse = await fetch(parentOrigin);
    const parentHtml = await parentResponse.text();
    assert.match(parentHtml, /<iframe[^>]+credentialless/);
    assert.match(parentHtml, /sandbox="allow-scripts allow-same-origin allow-forms"/);
    assert.match(parentHtml, new RegExp("frame\.contentWindow\.postMessage\\([\\s\\S]+, previewOrigin, \\[channel\.port2\\]\\)"));
    assert.match(parentResponse.headers.get('content-security-policy'), new RegExp('frame-src ' + previewOrigin.replaceAll('.', '\\.')));

    const brokerResponse = await fetch(previewOrigin + '/preview.html');
    const brokerHtml = await brokerResponse.text();
    assert.equal(brokerResponse.status, 200);
    assert.equal(brokerResponse.headers.get('set-cookie'), null);
    assert.equal(brokerResponse.headers.get('x-vibapp-preview-isolation'), 'partial');
    assert.match(brokerResponse.headers.get('content-security-policy'), new RegExp('frame-ancestors ' + parentOrigin.replaceAll('.', '\\.')));
    assert.match(brokerHtml, new RegExp(`data-parent-origin="${parentOrigin}"`));
    const guiManifest = JSON.parse(await readFile(new URL('../../product-platform/website/public/launcher/gui-sync-manifest.json', import.meta.url)));
    assert.match(brokerHtml, new RegExp(`data-gui-cache-version="${guiManifest.adapter.cache_version}"`));
    assert.match(brokerHtml, /data-preview-launcher/);
    assert.match(brokerHtml, /sandbox="allow-scripts allow-same-origin allow-forms"/);
    assert.doesNotMatch(brokerHtml, /src="\/launcher\/index\.html/);

    const cookieResponse = await rawRequest({
      port: previewPort,
      path: '/preview.html',
      headers: { Host: `127.0.0.1:${previewPort}`, Cookie: 'session=must-not-cross' },
    });
    assert.equal(cookieResponse.status, 400);
    assert.equal(cookieResponse.headers['set-cookie'], undefined);
    assert.deepEqual(JSON.parse(cookieResponse.body), { error: 'credentials-forbidden' });

    const authorizationResponse = await rawRequest({
      port: previewPort,
      path: '/launcher/index.html',
      headers: { Host: `127.0.0.1:${previewPort}`, Authorization: 'Bearer must-not-cross' },
    });
    assert.equal(authorizationResponse.status, 400);
    assert.equal(authorizationResponse.headers['set-cookie'], undefined);

    const serviceWorkerResponse = await rawRequest({
      port: previewPort,
      path: '/launcher/app-runtime-worker.js',
      headers: { Host: `127.0.0.1:${previewPort}`, 'Service-Worker': 'script' },
    });
    assert.equal(serviceWorkerResponse.status, 403);
    assert.equal(serviceWorkerResponse.headers['set-cookie'], undefined);

    const wrongHost = await rawRequest({
      port: previewPort,
      path: '/preview.html',
      headers: { Host: `localhost:${previewPort}` },
    });
    assert.equal(wrongHost.status, 421);
    assert.equal(wrongHost.headers['set-cookie'], undefined);

    const traversal = await rawRequest({
      port: previewPort,
      path: '/launcher/%2e%2e/server.mjs',
      headers: { Host: `127.0.0.1:${previewPort}` },
    });
    assert.notEqual(traversal.status, 200);

    const boundaryResponse = await fetch(previewOrigin + '/preview-boundary.json');
    assert.deepEqual(await boundaryResponse.json(), previewBoundary());
    assert.equal(boundaryResponse.headers.get('set-cookie'), null);

    const auditQuery = new URLSearchParams({
      schema_version: 'vibapp.invalid-bootstrap-report.experimental-v1',
      source: 'preview-frame',
      reason: 'wrong-nonce',
    });
    const auditResponse = await fetch(previewOrigin + '/audit/invalid-bootstrap.gif?' + auditQuery);
    assert.equal(auditResponse.status, 204);
    const auditLines = (await readFile(config.auditPath, 'utf8')).trim().split('\n');
    assert.equal(auditLines.length, 1);
    const auditEvent = JSON.parse(auditLines[0]);
    assert.equal(auditEvent.reason, 'wrong-nonce');
    assert.equal(auditEvent.source, 'preview-frame');
    assert.equal(auditEvent.result, 'rejected-before-authority');
    assert.doesNotMatch(auditLines[0], /payload|need|api.?key|cookie|token|session|user/i);
    const widenedAudit = await fetch(previewOrigin + '/audit/invalid-bootstrap.gif?' + auditQuery + '&payload=secret');
    assert.equal(widenedAudit.status, 400);
    assert.equal((await readFile(config.auditPath, 'utf8')).trim().split('\n').length, 1);

    const healthResponse = await fetch(previewOrigin + '/healthz');
    const health = await healthResponse.json();
    assert.equal(health.status, 'ok');
    assert.equal(health.invalid_bootstrap_audit.healthy, true);
    assert.equal(health.invalid_bootstrap_audit.payload_storage, 'forbidden');

    const launcherResponse = await fetch(previewOrigin + '/launcher/index.html');
    const source = await readFile(new URL('../../product-platform/website/public/launcher/index.html', import.meta.url));
    assert.deepEqual(Buffer.from(await launcherResponse.arrayBuffer()), source);
    assert.equal(launcherResponse.headers.get('set-cookie'), null);
    assert.equal(launcherResponse.headers.get('x-vibapp-preview-isolation'), 'partial');
    const launcherCsp = launcherResponse.headers.get('content-security-policy');
    assert.match(launcherCsp, /script-src 'self' 'wasm-unsafe-eval'/);
    assert.match(launcherCsp, /form-action 'none'/);
    assert.match(launcherCsp, /worker-src 'self'/);
    assert.doesNotMatch(launcherCsp, /script-src[^;]*(?:blob:|data:)/);
    assert.doesNotMatch(launcherCsp, /worker-src[^;]*(?:blob:|data:)/);
    assert.match(launcherResponse.headers.get('content-security-policy'), new RegExp("frame-ancestors 'self' " + parentOrigin.replaceAll('.', '\\.')));
    assert.match(brokerResponse.headers.get('content-security-policy'), /img-src 'self'/);

    const workerResponse = await fetch(previewOrigin + '/launcher/app-runtime-worker.js');
    assert.equal(workerResponse.status, 200);
    assert.match(workerResponse.headers.get('content-security-policy'), /script-src 'self' 'wasm-unsafe-eval'/);
    assert.doesNotMatch(workerResponse.headers.get('content-security-policy'), /script-src[^;]*(?:blob:|data:)/);

    const registry = JSON.parse(await readFile(new URL('../../product-platform/website/public/data/registry.snapshot.json', import.meta.url)));
    const binding = registry.browser_artifact_bindings[0];
    for (const descriptor of [...binding.files, binding.attestation.artifact]) {
      const response = await fetch(previewOrigin + descriptor.path);
      assert.equal(response.status, 200);
      assert.equal(response.headers.get('cache-control'), 'public, max-age=31536000, immutable');
    }

    const packageLocatorsResponse = await fetch(previewOrigin + '/data/package-locators/index.json');
    const packageLocatorsSource = await readFile(new URL('../../product-platform/website/public/data/package-locators/index.json', import.meta.url));
    assert.equal(packageLocatorsResponse.status, 200);
    assert.deepEqual(Buffer.from(await packageLocatorsResponse.arrayBuffer()), packageLocatorsSource);
    assert.equal(packageLocatorsResponse.headers.get('cache-control'), 'no-store');

    const unlistedDataResponse = await fetch(previewOrigin + '/data/package-locators/not-allowlisted.json');
    assert.equal(unlistedDataResponse.status, 404);
  } finally {
    await Promise.all([close(parent), close(preview)]);
    await rm(auditRoot, { recursive: true, force: true });
  }
});

test('component serving fails closed when content-addressed file bytes do not match the path digest', async () => {
  const publicRoot = await mkdtemp(join(tmpdir(), 'vibapp-preview-host-test-'));
  const component = 'a'.repeat(64);
  const advertised = '0'.repeat(64);
  const relative = join('launcher', 'components', component, 'module-' + advertised + '.mjs');
  await mkdir(join(publicRoot, 'launcher', 'components', component), { recursive: true });
  await writeFile(join(publicRoot, relative), 'export const mismatch = true;\n');
  const placeholder = createServer();
  const previewPort = await listen(placeholder);
  await close(placeholder);
  const previewOrigin = `http://127.0.0.1:${previewPort}`;
  const server = createPreviewServer(loadConfig({
    NODE_ENV: 'test',
    VIBAPP_PREVIEW_ORIGIN: previewOrigin,
    VIBAPP_PARENT_ORIGIN: 'http://127.0.0.1:4173',
    VIBAPP_PREVIEW_PORT: String(previewPort),
    VIBAPP_WEB_PUBLIC_ROOT: publicRoot,
    VIBAPP_PREVIEW_AUDIT_PATH: join(publicRoot, 'invalid-bootstrap-audit.jsonl'),
  }));
  await new Promise((resolve, reject) => server.listen(previewPort, '127.0.0.1').once('listening', resolve).once('error', reject));
  try {
    const response = await fetch(previewOrigin + '/' + relative.replaceAll('\\', '/'));
    assert.equal(response.status, 404);
  } finally {
    await close(server);
    await rm(publicRoot, { recursive: true, force: true });
  }
});

test('parent bootstrap binding is exact-origin, nonce-bound, one-port, and closed to extra authority fields', () => {
  const context = {
    eventOrigin: 'http://127.0.0.1:4173',
    expectedParentOrigin: 'http://127.0.0.1:4173',
    expectedPreviewOrigin: 'http://127.0.0.1:8788',
    expectedNonce: 'preview-frame-nonce-0123456789abcdef',
    portCount: 1,
    sourceIsParent: true,
  };
  const binding = {
    schema_version: 'vibapp.preview-frame-bind.experimental-v1',
    kind: 'bind-preview-frame',
    channel_id: 'preview-frame-channel-0123456789abcdef',
    nonce: context.expectedNonce,
    preview_origin: context.expectedPreviewOrigin,
  };
  assert.equal(validateParentBinding(binding, context), binding);
  assert.throws(() => validateParentBinding(binding, { ...context, eventOrigin: 'http://127.0.0.1:9999' }), /parent-origin/);
  assert.throws(() => validateParentBinding({ ...binding, nonce: binding.nonce + '-wrong' }, context), /nonce/);
  assert.throws(() => validateParentBinding(binding, { ...context, portCount: 2 }), /port-count/);
  assert.throws(() => validateParentBinding({ ...binding, install_authority: true }, context), /keys/);
  assert.equal(classifyParentBindingFailure(binding, { ...context, eventOrigin: 'http://127.0.0.1:9999' }), 'wrong-origin');
  assert.equal(classifyParentBindingFailure({ ...binding, nonce: binding.nonce + '-wrong' }, context), 'wrong-nonce');
  assert.equal(classifyParentBindingFailure({ ...binding, install_authority: true }, context), 'extra-authority');
  assert.equal(classifyParentBindingFailure({ ...binding, schema_version: 'tampered' }, context), 'tamper');
});

test('durable invalid-bootstrap audit is canonical, privacy-minimal, bounded, rotated, and recovers only a truncated tail', async () => {
  const root = await mkdtemp(join(tmpdir(), 'vibapp-invalid-bootstrap-audit-'));
  const path = join(root, 'events.jsonl');
  try {
    const audit = new DurableInvalidBootstrapAudit({
      path,
      maximumBytes: 4096,
      maximumEvents: 8,
    });
    await audit.initialize();
    for (const reason of ['wrong-origin', 'wrong-nonce', 'extra-authority', 'tamper', 'replay', 'wrong-origin', 'wrong-nonce', 'tamper', 'replay']) {
      await audit.record({ reason, source: 'preview-frame' });
    }
    const active = await readFile(path, 'utf8');
    const rotated = await readFile(path + '.1', 'utf8');
    assert.equal(active.trim().split('\n').length, 1);
    assert.equal(rotated.trim().split('\n').length, 8);
    for (const line of (rotated + active).trim().split('\n')) {
      const event = JSON.parse(line);
      assert.equal(line, canonicalAuditJson(event));
      assert.deepEqual(Object.keys(event).sort(), [
        'event_id',
        'observed_at_utc',
        'reason',
        'result',
        'schema_version',
        'source',
      ]);
      for (const forbidden of ['payload', 'need', 'api_key', 'cookie', 'token', 'session', 'user']) {
        assert.doesNotMatch(line, new RegExp(forbidden, 'i'));
      }
    }

    await appendFile(path, '{"partial":');
    const recovered = new DurableInvalidBootstrapAudit({ path, maximumBytes: 4096, maximumEvents: 8 });
    const status = await recovered.initialize();
    assert.equal(status.healthy, true);
    assert.equal(status.recovered_truncated_tail, true);
    assert.equal((await readFile(path, 'utf8')).trim().split('\n').length, 1);

    const retentionPath = join(root, 'retention.jsonl');
    const initialTime = new Date('2026-08-27T00:00:00Z');
    const retained = new DurableInvalidBootstrapAudit({
      path: retentionPath,
      retentionMilliseconds: 60_000,
      now: () => initialTime,
    });
    await retained.initialize();
    await retained.record({ reason: 'tamper', source: 'app-worker' });
    const expired = new DurableInvalidBootstrapAudit({
      path: retentionPath,
      retentionMilliseconds: 60_000,
      now: () => new Date('2026-08-27T00:02:00Z'),
    });
    await expired.initialize();
    assert.equal(await readFile(retentionPath, 'utf8'), '');
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('complete audit corruption makes preview activation fail closed', async () => {
  const root = await mkdtemp(join(tmpdir(), 'vibapp-invalid-bootstrap-corrupt-'));
  const path = join(root, 'events.jsonl');
  await writeFile(path, '{"complete":"but-invalid"}\n');
  const placeholder = createServer();
  const previewPort = await listen(placeholder);
  await close(placeholder);
  const previewOrigin = `http://127.0.0.1:${previewPort}`;
  const server = createPreviewServer(loadConfig({
    NODE_ENV: 'test',
    VIBAPP_PREVIEW_ORIGIN: previewOrigin,
    VIBAPP_PARENT_ORIGIN: 'http://127.0.0.1:4173',
    VIBAPP_PREVIEW_PORT: String(previewPort),
    VIBAPP_PREVIEW_AUDIT_PATH: path,
  }));
  await new Promise((resolve, reject) => server.listen(previewPort, '127.0.0.1').once('listening', resolve).once('error', reject));
  try {
    const response = await fetch(previewOrigin + '/preview.html');
    assert.equal(response.status, 503);
    assert.deepEqual(await response.json(), {
      error: 'invalid-bootstrap-audit-unavailable',
      preview_activation: 'fail-closed',
    });
  } finally {
    await close(server);
    await rm(root, { recursive: true, force: true });
  }
});

test('configuration fails closed for implicit local origins and any production origin drift', () => {
  assert.throws(() => loadConfig({ NODE_ENV: 'test' }), /VIBAPP_PREVIEW_ORIGIN-required/);
  assert.throws(() => loadConfig({
    NODE_ENV: 'test',
    VIBAPP_PREVIEW_ORIGIN: 'https://preview.vibapp.ai',
    VIBAPP_PARENT_ORIGIN: 'http://127.0.0.1:4173',
  }), /explicit-loopback/);
  assert.throws(() => loadConfig({
    NODE_ENV: 'production',
    VIBAPP_PREVIEW_ORIGIN: 'https://preview-staging.vibapp.ai',
    VIBAPP_PARENT_ORIGIN: 'https://vibapp.ai',
  }), /production-preview-origin/);
  assert.throws(() => loadConfig({
    NODE_ENV: 'production',
    VIBAPP_PREVIEW_ORIGIN: 'https://preview.vibapp.ai',
    VIBAPP_PARENT_ORIGIN: 'https://www.vibapp.ai',
  }), /production-parent-origin/);
  assert.throws(() => loadConfig({
    NODE_ENV: 'production',
    VIBAPP_PREVIEW_ORIGIN: 'https://preview.vibapp.ai',
    VIBAPP_PARENT_ORIGIN: 'https://vibapp.ai',
  }), /production-audit-path-required/);
});
