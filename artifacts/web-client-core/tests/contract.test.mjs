import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createHash, webcrypto } from 'node:crypto';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import test, { after } from 'node:test';
import { deriveBrowserComponent } from '../derive-browser-component.mjs';
import {
  buildRegistryProjection,
  isLoopbackLocalProjection,
  isPublicRegistryRecord,
  isRealPublicBrowserBinding,
  verifyPublicProjectionArtifacts,
} from '../sync-web-gui.mjs';
import { runPolicyFixtures } from '../activation-policy/validate-web-activation.mjs';
import { classifyInvalidBootstrapError } from '../invalid-bootstrap-audit.mjs';
import {
  verifyBrowserPreviewBinding,
  verifyControlEnvelope,
  verifyDerivationAttestation,
  verifyDerivationFileSet,
  verifyLaunchEnvelope,
} from '../runtime-integrity.mjs';

if (!globalThis.crypto) globalThis.crypto = webcrypto;

const root = new URL('../', import.meta.url);
const website = new URL('../../product-platform/website/', import.meta.url);
const publicRoot = new URL('public/', website);
const registrySource = new URL('../../product-platform/registry/snapshots/registry.snapshot.json', import.meta.url);

let browserFixtureRoot;
let browserFixturePromise;

async function browserFixture() {
  browserFixturePromise ||= (async () => {
    browserFixtureRoot = await mkdtemp(join(tmpdir(), 'vibapp-contract-browser-fixture-'));
    const derived = await deriveBrowserComponent(browserFixtureRoot);
    const binding = derived.binding;
    const record = derived.previewRecord;
    const files = new Map();
    for (const descriptor of binding.files) {
      const bytes = await readFile(join(
        browserFixtureRoot,
        descriptor.path.replace(/^\/launcher\//, ''),
      ));
      files.set(descriptor.path, new Uint8Array(bytes));
    }
    const attestation = await readFile(join(
      browserFixtureRoot,
      binding.attestation.artifact.path.replace(/^\/launcher\//, ''),
    ));
    return { binding, record, files, attestation, outputRoot: browserFixtureRoot };
  })();
  return browserFixturePromise;
}

after(async () => {
  if (browserFixtureRoot) await rm(browserFixtureRoot, { recursive: true, force: true });
});

test('bridge routes the shared GUI through the trusted same-product backend', async () => {
  const bridge = await readFile(new URL('web-bridge.js', root), 'utf8');
  for (const command of [
    'get_state',
    'submit_need',
    'complete_need',
    'get_model_settings',
    'save_model_settings',
    'get_codeagent_settings',
    'save_codeagent_settings',
    'submit_development_task',
    'submit_install_intent',
    'launch_app',
  ]) {
    assert.match(bridge, new RegExp("command === '" + command + "'"));
  }
  assert.match(bridge, /vibapp_web_abi_version\(\) !== 1/);
  assert.match(bridge, /browser_background_reliability: 'foreground-only'/);
  assert.match(bridge, /verifyBrowserPreviewBinding\(record, binding\)/);
  assert.match(bridge, /verifyDerivationAttestation\(binding, attestationBytes\)/);
  assert.match(bridge, /new MessageChannel\(\)/);
  assert.match(bridge, /vibapp\.web-parent-storage\.experimental-v1/);
  assert.match(bridge, /vibapp\.web-parent-storage-result\.experimental-v1/);
  assert.match(bridge, /vibapp\.web-parent-product\.experimental-v1/);
  assert.match(bridge, /vibapp\.web-parent-product-result\.experimental-v1/);
  assert.match(bridge, /event\.source !== parent/);
  assert.match(bridge, /event\.origin !== location\.origin/);
  assert.match(bridge, /MAX_STATE_BYTES = 1024 \* 1024/);
  assert.match(bridge, /window\.VibAppWebBridge = \{ invoke, ready: storageReady, setLocalePreference \}/);
  assert.match(bridge, /vibapp\.preview-port-bind\.experimental-v1/);
  assert.match(bridge, /verifyLaunchControl\(event\.data\?\.control/);
  assert.match(bridge, /parentProduct\('submit_need', payload\)/);
  assert.match(bridge, /parentProduct\('complete_need', payload\)/);
  assert.match(bridge, /parentProduct\('submit_development_task', payload\)/);
  assert.match(bridge, /parentProduct\('get_codeagent_settings', \{\}\)/);
  assert.match(bridge, /record\.publication\?\.state === 'published'/);
  assert.match(bridge, /record\.verification\?\.status === 'verified'/);
  assert.match(bridge, /record\.verification\?\.revocation === 'not-revoked'/);
  assert.match(bridge, /record\.source\?\.github_archive\?\.organization === 'vib-app'/);
  assert.match(bridge, /SHA256\.test\(record\.package\?\.package_digest_sha256 \|\| ''\)/);
  assert.match(bridge, /website_projection_mode === 'loopback-local-development'/);
  assert.match(bridge, /location\.hostname === '127\.0\.0\.1'/);
  assert.match(bridge, /isLoopbackPrivateBinding\(record, binding\)/);
  assert.doesNotMatch(bridge, /web-codeagent-task-preview/);
  assert.doesNotMatch(bridge, /waiting-for-codeagent-service|service-unconfigured/);
  assert.doesNotMatch(bridge, /CODEAGENT_TASK_PROVIDER/);
  assert.doesNotMatch(bridge, /component_sha256: record\.package\.package_digest_sha256/);
  assert.doesNotMatch(bridge, /serviceWorker\.register/);
});

test('trusted Website parent fetches public packages into a bounded cache without exposing transport authority', async () => {
  const [bridge, parent] = await Promise.all([
    readFile(new URL('web-bridge.js', root), 'utf8'),
    readFile(new URL('../../product-platform/website/app/preview-frame.tsx', import.meta.url), 'utf8'),
  ]);
  assert.match(parent, /createBrowserPackageStore\(\{ cacheLimitBytes \}\)/);
  assert.match(parent, /createRoomHashBrowserNode\(\{ candidateStore: store \}\)/);
  assert.match(parent, /fetchVerifiedCandidateToStore\(locator\.candidate\)/);
  assert.match(parent, /entries,registry_snapshot_sha256,schema_version,trust_note/);
  assert.match(parent, /registry_record_sha256/);
  assert.match(parent, /await sha256Hex\(registryDocument\.bytes\) !== index\.registry_snapshot_sha256/);
  assert.match(parent, /public-package-locator-digest/);
  assert.match(parent, /PUBLIC_TRACKERS\.has\(tracker\)/);
  assert.match(parent, /vibapp\.website-public-package-cache\.experimental-v1/);
  const publicReceipt = parent.slice(parent.indexOf("schema_version: 'vibapp.website-public-package-cache.experimental-v1'"), parent.indexOf('} finally {', parent.indexOf("schema_version: 'vibapp.website-public-package-cache.experimental-v1'")));
  assert.doesNotMatch(publicReceipt, /magnet|info_hash|locator_path|\bfiles\b|\bbytes\b/i);

  assert.match(bridge, /parentProduct\('ensure_public_app_available'/);
  assert.match(bridge, /status: 'cached-for-web'/);
  assert.match(bridge, /installation_performed: false/);
  assert.match(bridge, /web_package_cached: true/);
  const storedReceipt = bridge.slice(bridge.indexOf('cache_receipt: {'), bridge.indexOf('};', bridge.indexOf('cache_receipt: {')));
  assert.doesNotMatch(storedReceipt, /magnet|info_hash|locator_path|\bfiles\b|\bbytes\b/i);
  assert.doesNotMatch(parent, /serviceWorker\.register|URL\.createObjectURL/);
});

test('Website Registry projection excludes private candidates by default and gates public sharing on exact trust plus a real binding', async () => {
  const source = JSON.parse(await readFile(registrySource, 'utf8'));
  const production = buildRegistryProjection(source);
  assert.equal(production.consumer_notes.website_projection_mode, 'production-public-only');
  assert.equal(production.consumer_notes.local_private_preview_enabled, false);
  assert.deepEqual(production.browser_preview_records, []);
  assert.deepEqual(production.browser_artifact_bindings, []);
  assert.deepEqual(production.consumer_notes.website_public_shareable_app_ids, []);
  assert.equal(production.records.length, 0, 'synthetic conformance fixtures are not published apps');
  assert.ok(production.records.every(isPublicRegistryRecord));
  assert.ok(!JSON.stringify(production).includes('ai.vibapp.hello'));
  await verifyPublicProjectionArtifacts(production, fileURLToPath(publicRoot));

  const fixture = await browserFixture();
  const local = buildRegistryProjection(source, {
    binding: fixture.binding,
    previewRecord: fixture.record,
  });
  assert.equal(local.consumer_notes.website_projection_mode, 'loopback-local-development');
  assert.equal(local.consumer_notes.local_private_preview_enabled, true);
  assert.equal(local.browser_preview_records.length, 1);
  assert.equal(local.browser_preview_records[0].publication.state, 'private-candidate');
  assert.equal(local.browser_artifact_bindings.at(-1).attestation.stage0_activation_eligible, false);

  const publicRecord = structuredClone(source.records.at(-1));
  publicRecord.record_id = 'registry.public.ai-vibapp-hello.1';
  publicRecord.record_revision = 1;
  publicRecord.app.id = fixture.record.app.id;
  publicRecord.app.version = fixture.record.app.version;
  publicRecord.app.publisher.publisher_id = 'publisher.example';
  publicRecord.verification.evidence = [{ evidence_id: 'example.verify.1', kind: 'verifier-report', sha256: '2'.repeat(64) }];
  publicRecord.package.app_id = fixture.record.app.id;
  publicRecord.package.version = fixture.record.app.version;
  publicRecord.package.package_digest_sha256 = fixture.binding.canonical_package_digest_sha256;
  publicRecord.source.github_archive = {
    organization: 'vib-app', repository: 'app-' + createHash('sha256')
      .update(`publisher.example\n${publicRecord.app.id}`).digest('hex'),
    repository_id: 12345, commit_sha: 'a'.repeat(40),
    source_digest_sha256: publicRecord.source.source_digest_sha256,
    package_digest_sha256: publicRecord.package.package_digest_sha256,
  };
  publicRecord.compatibility = {
    profiles: ['web-preview'],
    platforms: [{ os: 'browser', arch: 'wasm32', profile: 'web-preview' }],
  };
  const publicBinding = structuredClone(fixture.binding);
  publicBinding.attestation.verification_state = 'verified';
  publicBinding.attestation.stage0_activation_eligible = true;
  assert.equal(isRealPublicBrowserBinding(publicRecord, publicBinding), true);
  const withPublicRuntime = buildRegistryProjection({
    ...source,
    records: [...source.records, publicRecord],
    browser_artifact_bindings: [...source.browser_artifact_bindings, publicBinding],
  });
  assert.deepEqual(withPublicRuntime.consumer_notes.website_public_shareable_app_ids, ['ai.vibapp.hello']);
  const emptyPublicRoot = await mkdtemp(join(tmpdir(), 'vibapp-empty-public-root-'));
  try {
    await assert.rejects(
      verifyPublicProjectionArtifacts(withPublicRuntime, emptyPublicRoot),
      /Public browser binding artifact is not a regular file/,
    );
  } finally {
    await rm(emptyPublicRoot, { recursive: true, force: true });
  }

  const privateRegistryRecord = structuredClone(fixture.record);
  privateRegistryRecord.app.id = 'ai.vibapp.private.leak-probe';
  privateRegistryRecord.package.app_id = privateRegistryRecord.app.id;
  privateRegistryRecord.publication.state = 'private-candidate';
  const withPrivateRecord = buildRegistryProjection({
    ...source,
    records: [...source.records, privateRegistryRecord],
  });
  assert.ok(!withPrivateRecord.records.some(record => record.app.id === privateRegistryRecord.app.id));

  assert.equal(isLoopbackLocalProjection({ npm_lifecycle_event: 'prebuild:local' }), true);
  assert.equal(isLoopbackLocalProjection({
    npm_lifecycle_event: 'prebuild',
    VIBAPP_WEB_PRIVATE_PREVIEW_MODE: 'loopback-local-development',
    VIBAPP_PREVIEW_LOCAL: '1',
    VIBAPP_PREVIEW_ORIGIN: 'http://127.0.0.1:4174',
  }), false);
  assert.equal(isLoopbackLocalProjection({
    VIBAPP_WEB_PRIVATE_PREVIEW_MODE: 'loopback-local-development',
    VIBAPP_PREVIEW_LOCAL: '1',
    VIBAPP_PREVIEW_ORIGIN: 'http://127.0.0.1:4174',
  }), true);
  assert.equal(isLoopbackLocalProjection({
    VIBAPP_WEB_PRIVATE_PREVIEW_MODE: 'loopback-local-development',
    VIBAPP_PREVIEW_LOCAL: '1',
    VIBAPP_PREVIEW_ORIGIN: 'https://preview.vibapp.ai',
  }), false);
});

test('app Worker imports only reverified Jco ESM bytes and remains foreground-only', async () => {
  const worker = await readFile(new URL('app-runtime-worker.js', root), 'utf8');
  assert.match(worker, /verifyLaunchEnvelope\(request\)/);
  assert.match(worker, /binding\.port instanceof MessagePort/);
  assert.match(worker, /verifyLaunchControl\(message\?\.control/);
  assert.match(worker, /port\.postMessage/);
  assert.match(worker, /if \(consumed\)/);
  assert.match(worker, /verifyDerivationFileSet\(request\.binding, values\)/);
  assert.match(worker, /await import\(entryUrl\.href\)/);
  assert.match(worker, /component\.guest\.describe\(\)/);
  assert.match(worker, /component\.guest\.handleEvent/);
  assert.match(worker, /dedicated_worker: true/);
  assert.match(worker, /background_reliability: 'foreground-only'/);
  assert.match(worker, /stage0_activation_eligible: false/);
  assert.match(worker, /reportInvalidBootstrap\(reason, 'app-worker'\)/);
  assert.match(worker, /PORT_BIND_KEYS/);
  assert.doesNotMatch(worker, /WebAssembly\.instantiate\(request\.artifact_bytes/);
  assert.doesNotMatch(worker, /new Blob|createObjectURL|blob:|data:|document\.|window\.|serviceWorker/);
  assert.doesNotMatch(worker, /self\.postMessage/);
});

test('product-only Web activation proposal is executable and rejects every required negative fixture', async () => {
  const report = await runPolicyFixtures();
  assert.equal(report.state, 'local-product-proposal-validation-pass');
  assert.equal(report.positive_cases, 1);
  assert.equal(report.negative_cases, 7);
  assert.equal(report.proposal_schema, 'draft-2020-12-parsed-and-cross-checked');
  assert.equal(report.current_product_activation_eligible, false);
  assert.equal(report.formal_stage0_acceptance, 'not-claimed');
  assert.equal(report.public_activation, 'not-authorized');
  assert.ok(report.negative_results.some(item => item.case_id === 'runtime-same-origin-sandbox' && item.reason === 'runtime-origin-isolation'));
  assert.ok(report.negative_results.some(item => item.case_id === 'runtime-audit-unhealthy' && item.reason === 'runtime-audit'));
});

test('invalid bootstrap classification is closed to the five canonical privacy-safe reasons', () => {
  assert.equal(classifyInvalidBootstrapError(new Error('preview-bind-rejected:parent-origin')), 'wrong-origin');
  assert.equal(classifyInvalidBootstrapError(new Error('integrity-failure:control-nonce-mismatch')), 'wrong-nonce');
  assert.equal(classifyInvalidBootstrapError(new Error('integrity-failure:control-keys')), 'extra-authority');
  assert.equal(classifyInvalidBootstrapError(new Error('integrity-failure:control-sequence-mismatch')), 'replay');
  assert.equal(classifyInvalidBootstrapError(new Error('integrity-failure:attestation-artifact-digest-mismatch')), 'tamper');
  assert.equal(classifyInvalidBootstrapError(new Error('ordinary-network-failure')), null);
});

test('preview control messages are nonce-bound, ordered, and reject replay-shaped drift', () => {
  const control = {
    schema_version: 'vibapp.preview-control.experimental-v1',
    user_id: 'anonymous-web-preview',
    app_id: 'ai.vibapp.hello',
    artifact_digest: 'a'.repeat(64),
    generation_id: 'web-generation-0123456789abcdef01234567',
    session_id: 'web-session-0123456789abcdef',
    view_revision: 1,
    event_id: 'preview-event-0123456789abcdef',
    channel_id: 'preview-channel-0123456789abcdef',
    nonce: 'preview-nonce-0123456789abcdef',
    request_id: 'preview-request-0123456789abcdef',
    sequence: 1,
    kind: 'launch',
  };
  verifyControlEnvelope(control, control);
  assert.throws(
    () => verifyControlEnvelope({ ...control, nonce: 'preview-nonce-fedcba9876543210' }, control),
    /integrity-failure:control-nonce-mismatch/,
  );
  assert.throws(
    () => verifyControlEnvelope({ ...control, sequence: 2 }, control),
    /integrity-failure:control-sequence-mismatch/,
  );
  assert.throws(
    () => verifyControlEnvelope({ ...control, request_id: control.request_id + '-replay' }, control),
    /integrity-failure:control-request-id-mismatch/,
  );
  assert.throws(
    () => verifyControlEnvelope({ ...control, ambient_authority: true }, control),
    /integrity-failure:control-keys/,
  );
});

test('shared GUI requires a separate external CodeAgent cost acknowledgement', async () => {
  const sharedGui = await readFile(new URL('../../desktop/ui/app.js', import.meta.url), 'utf8');
  assert.match(sharedGui, /data-codeagent-submit-confirmation/);
  assert.match(sharedGui, /data-external-cost-confirmation/);
  assert.match(sharedGui, /const externalCostAcknowledged =/);
  assert.match(sharedGui, /acknowledge_external_cost: true/);
});

test('canonical Component, static Jco files, binding, and attestation are exact and tamper-evident', async () => {
  const { binding, record, files, attestation } = await browserFixture();
  await verifyBrowserPreviewBinding(record, binding);
  await verifyDerivationFileSet(binding, files);
  const attestationDocument = await verifyDerivationAttestation(binding, attestation);
  assert.equal(binding.entry.format, 'jco-esm');
  assert.equal(binding.files.length, 2);
  assert.equal(binding.host_adapter.format, 'host-adapter-esm');
  assert.equal(binding.derived_from_sha256, binding.canonical_component.sha256);
  assert.equal(binding.attestation.canonical_component_transformation_proven, true);
  assert.equal(binding.attestation.stage0_activation_eligible, false);
  assert.equal(attestationDocument.derivation.tool.version, '1.15.4');
  assert.equal(attestationDocument.profile_boundary.canonical_manifest_profiles.join(','), 'desktop');
  assert.match(attestationDocument.profile_boundary.blocker, /canonical-manifest/);

  const canonical = await readFile(new URL(
    '../../app-builder/demo-output/pipeline/candidates/dfad1fed5fe0eb8c5bf83927eee64695287e22b5c8149d878b231d69d056e674/package/component.wasm',
    import.meta.url,
  ));
  assert.equal(canonical.byteLength, binding.canonical_component.size_bytes);
  assert.equal(createHash('sha256').update(canonical).digest('hex'), binding.canonical_component.sha256);

  const request = {
    schema_version: 'vibapp.web-worker-launch.experimental-v1',
    app_id: binding.app_id,
    session: 'web-session-0123456789abcdef',
    profile: binding.profile,
    package_digest_sha256: binding.canonical_package_digest_sha256,
    component_sha256: binding.canonical_component.sha256,
    browser_artifact_sha256: binding.entry.sha256,
    entry_path: binding.entry.path,
    binding,
  };
  await verifyLaunchEnvelope(request);

  const tamperedArtifact = files.get(binding.entry.path).slice();
  tamperedArtifact[tamperedArtifact.length - 1] ^= 1;
  const tamperedFiles = new Map(files);
  tamperedFiles.set(binding.entry.path, tamperedArtifact);
  await assert.rejects(verifyDerivationFileSet(binding, tamperedFiles), /integrity-failure:derivation-file-digest-mismatch/);
  const tamperedAttestation = attestation.slice();
  tamperedAttestation[tamperedAttestation.length - 2] ^= 1;
  await assert.rejects(verifyDerivationAttestation(binding, tamperedAttestation), /integrity-failure:attestation-artifact-digest-mismatch/);
  const widened = structuredClone(binding);
  widened.attestation.stage0_activation_eligible = true;
  await assert.rejects(verifyBrowserPreviewBinding(record, widened), /integrity-failure:attestation-stage0-boundary/);
  const wrongPackage = structuredClone(binding);
  wrongPackage.canonical_package_digest_sha256 = '0'.repeat(64);
  await assert.rejects(verifyBrowserPreviewBinding(record, wrongPackage), /integrity-failure:(attestation-binding-payload|record-package-digest)/);
});

test('exact derived Jco module executes the canonical guest contract locally', async () => {
  const { binding, files, outputRoot } = await browserFixture();
  const artifactUrl = pathToFileURL(join(outputRoot, binding.entry.path.replace(/^\/launcher\//, '')));
  const component = await import(artifactUrl.href + '?test=' + Date.now());
  const descriptor = component.guest.describe();
  assert.deepEqual(
    { id: descriptor.id, version: descriptor.version, kind: descriptor.kind, displayName: descriptor.displayName },
    { id: 'ai.vibapp.hello', version: '0.1.0', kind: 'ui', displayName: 'Hello VibApp' },
  );
  const session = 'web-session-0123456789abcdef';
  const output = component.guest.handleEvent({
    eventId: 'web-event-0123456789abcdef',
    idempotencyKey: 'web-idempotency-0123456789abcdef',
    cancellation: 'web-cancellation-0123456789abcdef',
    generation: 'web-generation-0123456789abcdef',
    profile: 'web-preview',
    deadlineMonotonicMs: 2_000n,
  }, {
    tag: 'launcher',
    val: { tag: 'launch', val: { entrypoint: 'main', session, surface: 'surface-main', route: 'home', reason: 'deep-link' } },
  });
  assert.equal(output.surfaces[0].session, session);
  assert.equal(output.surfaces[0].view.title, 'Hello VibApp');
  assert.match(output.surfaces[0].view.nodes[1].kind.val.text, /Hello from a generated VibApp/);
  assert.equal(createHash('sha256').update(files.get(binding.entry.path)).digest('hex'), binding.entry.sha256);
});

test('offline Jco derivation is deterministic for the exact promoted candidate', async () => {
  const generated = await mkdtemp(join(tmpdir(), 'vibapp-derive-test-'));
  try {
    const result = await deriveBrowserComponent(generated);
    const fixture = await browserFixture();
    assert.equal(result.binding.entry.sha256, fixture.binding.entry.sha256);
    assert.equal(result.binding.entry.size_bytes, fixture.binding.entry.size_bytes);
    assert.deepEqual(result.binding.files, fixture.binding.files);
    assert.equal(result.binding.attestation.binding_payload_sha256, fixture.binding.attestation.binding_payload_sha256);
    assert.equal(result.binding.attestation.artifact.sha256, fixture.binding.attestation.artifact.sha256);
  } finally {
    await rm(generated, { recursive: true, force: true });
  }
});

test('separate local verifier freshly rederives and preserves the non-Stage-0 boundary', async t => {
  const snapshot = JSON.parse(await readFile(new URL('data/registry.snapshot.json', publicRoot), 'utf8'));
  if (snapshot.consumer_notes?.website_projection_mode !== 'loopback-local-development') {
    t.skip('the separate published-output verifier is available only after the explicit loopback-local sync');
    return;
  }
  const verifier = new URL('../verify-browser-derivation.mjs', import.meta.url);
  const result = spawnSync(process.execPath, [verifier.pathname], {
    cwd: new URL('../../../', import.meta.url),
    env: { ...process.env, npm_config_offline: 'true' },
    encoding: 'utf8',
    maxBuffer: 8 * 1024 * 1024,
  });
  assert.equal(result.status, 0, result.stderr || result.stdout);
  const report = JSON.parse(result.stdout);
  assert.equal(report.state, 'local-independent-browser-derivation-verifier-pass');
  assert.equal(report.verifier_process, 'separate-node-child-rederivation');
  assert.equal(report.stage0_activation_eligible, false);
  assert.equal(report.formal_stage0_acceptance, 'not-claimed');
  assert.ok(report.checks.includes('blob-and-data-module-imports-absent'));
  assert.ok(report.remaining_formal_blockers.length >= 3);
});

test('website launcher is generated from the exact Desktop GUI source with a hashed adapter manifest', async () => {
  const desktop = new URL('../../desktop/ui/', import.meta.url);
  const generated = new URL('../../product-platform/website/public/launcher/', import.meta.url);
  const syncManifest = JSON.parse(await readFile(new URL('gui-sync-manifest.json', generated), 'utf8'));
  const sharedGui = await readFile(new URL('app.js', desktop), 'utf8');
  assert.match(sharedGui, /await window\.VibAppWebBridge\?\.ready/);
  assert.match(sharedGui, /setLocalePreference\?\.\(preference\)/);
  for (const lifecycleFact of [
    'data-install-app',
    'data-app-action="update"',
    'available_update',
    'rollback_reason',
    'control_app_lifecycle',
    'get_codeagent_settings',
    'save_codeagent_settings',
    'service-start',
    'service-stop',
    'status',
    'uninstall',
    'surface-close',
  ]) {
    assert.match(sharedGui, new RegExp(lifecycleFact), `shared GUI is missing ${lifecycleFact}`);
  }
  for (const name of ['app.js', 'styles.css', 'favicon.svg']) {
    const sourceBytes = await readFile(new URL(name, desktop));
    const generatedBytes = await readFile(new URL(name, generated));
    assert.deepEqual(generatedBytes, sourceBytes, `${name} drifted from the single Desktop GUI source`);
    const digest = createHash('sha256').update(sourceBytes).digest('hex');
    assert.deepEqual(syncManifest.files[name], { source_sha256: digest, generated_sha256: digest, exact_match: true });
  }
  for (const name of ['runtime-integrity.mjs', 'app-runtime-worker.js', 'invalid-bootstrap-audit.mjs', 'web-bridge.js']) {
    assert.deepEqual(
      await readFile(new URL(name, generated)),
      await readFile(new URL('../' + name, import.meta.url)),
      `${name} drifted from its reviewed Web adapter source`,
    );
  }
  assert.deepEqual(
    await readFile(new URL('../../product-platform/website/public/roomhash/roomhash-browser-node.mjs', import.meta.url)),
    await readFile(new URL('../roomhash-browser-node.mjs', import.meta.url)),
    'trusted Website RoomHash browser adapter drifted from its reviewed source',
  );
  const desktopHtml = await readFile(new URL('index.html', desktop), 'utf8');
  const generatedHtml = await readFile(new URL('index.html', generated), 'utf8');
  const bridge = syncManifest.adapter.bridge;
  const cacheVersion = bridge.split('?v=')[1];
  const expectedCacheHash = createHash('sha256');
  expectedCacheHash.update(await readFile(new URL('app.js', desktop)));
  expectedCacheHash.update(await readFile(new URL('../web-bridge.js', import.meta.url)));
  assert.equal(cacheVersion, expectedCacheHash.digest('hex').slice(0, 16), 'Web GUI cache token is not source-derived');
  assert.equal(syncManifest.adapter.cache_version, cacheVersion);
  const expectedHtml = desktopHtml
    .replace('<small data-locale-key="app_mode_label">LOCAL</small>', '<small data-locale-key="app_mode_label">WEB · WASM</small>')
    .replace('<script src="app.js" defer></script>', `<script type="module" src="${bridge}"></script>\n    <script src="app.js?v=${cacheVersion}" defer></script>`);
  assert.equal(generatedHtml, expectedHtml, 'website index contains edits outside the Web bridge injection');
  await assert.rejects(readFile(new URL('weather-preview.core.wasm', generated)));
});

test('credentialless preview delegates bounded state and allowlisted product calls to the trusted Website parent', async () => {
  const parent = await readFile(new URL('app/preview-frame.tsx', website), 'utf8');
  const broker = await readFile(new URL('../../web-preview-host/preview-broker.mjs', import.meta.url), 'utf8');
  assert.match(parent, /credentialless/);
  assert.match(parent, /sandbox="allow-scripts allow-same-origin allow-forms"/);
  assert.match(parent, /vibapp\.web-launcher\.state\.v1/);
  assert.match(parent, /vibapp\.ui_locale/);
  assert.match(parent, /1024 \* 1024/);
  assert.match(parent, /Object\.keys\(message\)\.sort\(\)\.join\(','\) !== 'key,kind,request_id,schema_version,value'/);
  assert.match(parent, /MAX_PRODUCT_MESSAGE_BYTES = 512 \* 1024/);
  assert.match(parent, /vibapp\.web-parent-product\.experimental-v1/);
  assert.match(parent, /PRODUCT_COMMANDS = new Set/);
  assert.match(parent, /fetch\('\/api\/product'/);
  assert.match(parent, /credentials: 'omit'/);
  assert.match(parent, /channel\.port1\.postMessage/);
  assert.match(parent, /\/roomhash\/roomhash-browser-node\.mjs/);
  assert.match(parent, /BROWSER_NETWORK_COMMANDS/);
  assert.match(parent, /website-browser-foreground/);
  assert.match(broker, /vibapp\.web-storage-bind\.experimental-v1/);
  assert.match(broker, /\[port\]/);
  const previewHost = await readFile(new URL('../../web-preview-host/server.mjs', import.meta.url), 'utf8');
  assert.match(previewHost, /sandbox="allow-scripts allow-same-origin allow-forms"/);
  assert.match(previewHost, /form-action 'none'/);
  const websiteConfig = await readFile(new URL('next.config.ts', website), 'utf8');
  assert.match(websiteConfig, /form-action 'none'/);
  assert.match(websiteConfig, /wss:\/\/tracker\.webtorrent\.dev/);
  assert.match(websiteConfig, /wss:\/\/tracker\.openwebtorrent\.com/);
  assert.match(websiteConfig, /wss:\/\/tracker\.btorrent\.xyz/);
  assert.doesNotMatch(previewHost, /tracker\.webtorrent\.dev|tracker\.openwebtorrent\.com|tracker\.btorrent\.xyz/);
  assert.doesNotMatch(parent, /Cookie|Authorization|api.?key/i);
});

test('Website product route keeps the backend token server-side and reuses Desktop Rust modules', async () => {
  const route = await readFile(new URL('app/api/product/route.ts', website), 'utf8');
  const helper = await readFile(new URL('../../desktop/src-tauri/src/bin/vibapp-product-bridge.rs', import.meta.url), 'utf8');
  const backend = await readFile(new URL('../../web-product-backend/server.mjs', import.meta.url), 'utf8');
  assert.match(route, /process\.env\.VIBAPP_PRODUCT_BACKEND_TOKEN/);
  assert.match(route, /Authorization: `Bearer \$\{target\.token\}`/);
  assert.match(route, /credentials: 'omit'/);
  assert.match(route, /backend-unconfigured/);
  assert.doesNotMatch(route, /NEXT_PUBLIC|PUBLIC_/);
  for (const module of [
    'model_settings',
    'codeagent_settings',
    'network_settings',
    'registry_integration',
    'delivery_integration',
    'local_product',
  ]) assert.match(helper, new RegExp(`mod ${module}`));
  assert.match(helper, /"get_network_settings"\s*=>\s*Ok\(\(network_settings::get/);
  assert.match(helper, /"save_network_settings"\s*=>\s*Ok\(\(/);
  assert.match(helper, /submit_and_start/);
  assert.match(helper, /runtime_from_task/);
  assert.match(backend, /request\.headers\.cookie/);
  assert.match(backend, /request\.headers\.origin/);
  assert.match(backend, /timingSafeEqual/);
  assert.doesNotMatch(backend, /VIBAPP_WEB_PRODUCT_TOKEN.*environment\[/s);
});

test('share route admits only published verified public records with a real binding, plus explicit loopback-local private preview', async () => {
  const page = await readFile(new URL('app/apps/[appId]/page.tsx', website), 'utf8');
  assert.match(page, /record\.publication\?\.state === 'published'/);
  assert.match(page, /record\.verification\?\.status === 'verified'/);
  assert.match(page, /record\.verification\?\.revocation === 'not-revoked'/);
  assert.match(page, /record\.source\?\.github_archive\?\.organization === 'vib-app'/);
  assert.match(page, /isRealPublicBrowserBinding\(record, binding\)/);
  assert.match(page, /website_public_shareable_app_ids/);
  assert.match(page, /process\.env\.VIBAPP_PREVIEW_LOCAL !== '1'/);
  assert.match(page, /origin\.hostname === '127\.0\.0\.1'/);
  assert.match(page, /website_projection_mode === 'loopback-local-development'/);
  assert.match(page, /record\.publication\?\.state === 'private-candidate'/);
  assert.match(page, /if \(!record\) notFound\(\)/);
  assert.match(page, /<PreviewFrame appId=\{appId\}/);
  const originHelper = await readFile(new URL('lib/preview-origin.ts', website), 'utf8');
  assert.match(originHelper, /https:\/\/preview\.vibapp\.ai/);
  assert.match(originHelper, /parsed\.hostname === '127\.0\.0\.1'/);
  assert.match(originHelper, /VIBAPP_PREVIEW_LOCAL/);
  const previewFrame = await readFile(new URL('app/preview-frame.tsx', website), 'utf8');
  assert.match(previewFrame, /new URL\('\/preview\.html', previewOrigin\)/);
  assert.match(previewFrame, /broker\.searchParams\.set\('app', appId\)/);
  assert.match(previewFrame, /broker\.hash = 'nonce='/);
  assert.match(previewFrame, /new MessageChannel\(\)/);
  assert.match(previewFrame, /postMessage\([\s\S]+previewOrigin, \[channel\.port2\]\)/);
});
