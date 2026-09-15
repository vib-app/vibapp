import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const digest = 'a'.repeat(64);
const component = 'b'.repeat(64);
const appId = 'ai.vibapp.projection-test';

function desktopApp(patch = {}) {
  return { app_id: appId, package_digest_sha256: digest, display_name: 'Desktop app',
    kind: 'ui', publication_state: 'private-candidate', installation_state: 'installed',
    install_eligible: true, launch_eligible: true, launch_mode: 'native-daemon',
    active_profile: 'desktop', update_eligible: true, available_update: { version: '2' },
    update: { last_transaction: { status: 'succeeded' } }, web_cache_eligible: true,
    web_package_cached: true, web_runtime_available: true, guest_execution_performed: true,
    active_service_entrypoints: ['service'], service_statuses: { service: { state: 'running' } }, ...patch };
}

function record() {
  return { document_type: 'registry-record', record_id: 'record-projection', record_revision: 1,
    app: { id: appId, display_name: 'Registry app', version: '1', kind: 'ui', summary: 'Test', publisher: { display_name: 'Publisher' } },
    package: { app_id: appId, package_digest_sha256: digest },
    publication: { state: 'published' }, verification: { status: 'verified', revocation: 'not-revoked' },
    source: { visibility: 'public', source_digest_sha256: component, github_archive: {
      organization: 'vib-app', repository: 'app-' + component, repository_id: 1,
      commit_sha: 'c'.repeat(40), source_digest_sha256: component, package_digest_sha256: digest,
    } }, compatibility: { profiles: ['desktop', 'web-runtime'] }, permissions: [] };
}

function binding() {
  const prefix = '/launcher/components/' + component + '/';
  const entry = { path: prefix + 'component.js', media_type: 'text/javascript', sha256: 'd'.repeat(64), size_bytes: 100, format: 'jco-esm' };
  return { schema_version: 'vibapp.browser-derivation-binding.experimental-v1', source_kind: 'verifier-promoted-candidate',
    app_id: appId, canonical_package_digest_sha256: digest, profile: 'web-runtime',
    canonical_component: { media_type: 'application/wasm', sha256: component }, derived_from_sha256: component,
    entry, files: [entry], attestation: { verification_state: 'verified', canonical_component_transformation_proven: true,
      stage0_activation_eligible: true, binding_payload_sha256: 'e'.repeat(64),
      artifact: { path: prefix + 'attestation.json', media_type: 'application/json', sha256: 'f'.repeat(64), size_bytes: 100 } } };
}

function locator() {
  return { app_id: appId, record_id: 'record-projection', record_revision: 1,
    package_digest_sha256: digest, locator_path: '/data/package-locators/' + digest + '.json',
    locator_sha256: 'd'.repeat(64), registry_record_sha256: 'e'.repeat(64), size_bytes: 100, web_runtime_available: true };
}

function cache(patch = {}) {
  return { app_id: appId, package_digest_sha256: digest, status: 'cached-for-web', web_package_cached: true, web_runtime_available: true, ...patch };
}

function fixture({ records = [], bindings = [], locators = [], previews = [], local = false, hosted = false, hostname = '127.0.0.1' } = {}) {
  const source = readFileSync(new URL('../web-bridge.js', import.meta.url), 'utf8');
  const projection = source.slice(source.indexOf('function shortPermission('), source.indexOf('\nasync function submitNeed('));
  const exact = source.slice(source.indexOf('function exactKeys('), source.indexOf('\nconst parentStorageBinding'));
  const invoke = source.slice(source.indexOf('async function invoke('), source.indexOf('\nasync function setLocalePreference('));
  const registry = { records, browser_artifact_bindings: bindings, browser_preview_records: previews,
    consumer_notes: { website_projection_mode: local ? 'loopback-local-development' : 'production-public-only', local_private_preview_enabled: local } };
  let product = {}, state = { needs: [], installIntents: [] };
  const sent = [];
  const context = vm.createContext({ registry, hostedShellOnly: hosted, publicLocatorIndex: { entries: locators }, SHA256: /^[0-9a-f]{64}$/,
    location: { protocol: 'http:', hostname, port: '3000' },
    storageReady: Promise.resolve(), initialize: async () => {}, wasm: { sha256: component },
    readState: async () => state,
    parentProduct: async (command, payload) => { sent.push({ command, payload }); if (command !== 'get_state') throw new Error('unexpected external mutation'); return product; },
  });
  vm.runInContext(exact + projection + invoke, context);
  return { registry, sent, project: (...args) => context.webApps(...args),
    getState: (apps, intents = []) => { product = { apps }; state = { needs: [], installIntents: intents }; return context.invoke('get_state'); },
    invoke: (...args) => context.invoke(...args) };
}

test('desktop product rows are metadata only, never browser lifecycle authority', async () => {
  const f = fixture();
  const app = desktopApp(), original = JSON.stringify(app);
  const result = await f.getState([app], [cache()]);
  const projected = result.apps[0];
  assert.equal(projected.app_id, app.app_id);
  assert.equal(projected.package_digest_sha256, digest);
  assert.equal(projected.publication_state, 'private-candidate');
  for (const key of ['install_eligible', 'launch_eligible', 'web_cache_eligible', 'web_package_cached', 'web_runtime_available', 'update_eligible', 'guest_execution_performed']) assert.equal(projected[key], false, key);
  assert.equal(projected.installation_state, 'client-required');
  assert.equal(projected.launch_mode, 'client-required');
  assert.equal(projected.web_unavailable_reason, 'browser-runtime-unavailable');
  assert.equal(projected.active_profile, null);
  assert.equal(projected.available_update, null);
  assert.equal(projected.update, null);
  assert.equal(projected.active_service_entrypoints.length, 0);
  assert.equal(Object.keys(projected.service_statuses).length, 0);
  assert.equal(JSON.stringify(app), original);
  assert.equal(f.sent.length, 1);
  assert.equal(f.sent[0].command, 'get_state');
  assert.equal(f.project([null, [], {}, { app_id: null }], []).length, 0);
});

test('hosted browser launch needs exact verified binding and public locator, never just a WASM claim', () => {
  const good = fixture({ hosted: true, records: [record()], bindings: [binding()], locators: [locator()] });
  assert.equal(good.project([], [])[0].launch_eligible, true);
  for (const options of [ { bindings: [], locators: [locator()] }, { bindings: [binding()], locators: [] } ]) {
    const f = fixture({ hosted: true, records: [record()], ...options });
    assert.equal(f.project([], [])[0].launch_eligible, false);
  }
  const forged = fixture({ hosted: true });
  assert.equal(forged.project([desktopApp({ browser_runtime_available: true })], [])[0].launch_eligible, false);
});

test('public verified package cache stays available without inventing a runtime binding', () => {
  const f = fixture({ records: [record()], locators: [locator()] });
  const uncached = f.project([desktopApp()], [])[0];
  assert.equal(uncached.install_eligible, true);
  assert.equal(uncached.web_cache_eligible, true);
  assert.equal(uncached.launch_eligible, false);
  const cached = f.project([desktopApp()], [cache()])[0];
  assert.equal(cached.installation_state, 'cached');
  assert.equal(cached.web_package_cached, true);
  assert.equal(cached.web_runtime_available, false);
  assert.equal(cached.launch_eligible, false);
  assert.equal(f.project([], [cache({ package_digest_sha256: '9'.repeat(64) })])[0].installation_state, 'candidate');
});

test('verified public browser binding wins over contradictory desktop flags, revoked or mismatched bindings do not', () => {
  const publicRecord = record(), realBinding = binding();
  const f = fixture({ records: [publicRecord], bindings: [realBinding], locators: [locator()] });
  let projected = f.project([desktopApp({ launch_eligible: false })], [cache()])[0];
  assert.equal(projected.launch_eligible, true);
  assert.equal(projected.launch_mode, 'web-worker-foreground');
  assert.equal(projected.web_runtime_available, true);
  assert.equal(projected.web_unavailable_reason, null);
  realBinding.canonical_package_digest_sha256 = '9'.repeat(64);
  projected = f.project([desktopApp()], [cache()])[0];
  assert.equal(projected.launch_eligible, false);
  assert.equal(projected.web_runtime_available, false);
  publicRecord.verification.revocation = 'revoked';
  projected = f.project([desktopApp()], [cache()])[0];
  assert.equal(projected.installation_state, 'client-required');
  assert.equal(projected.web_package_cached, false);
  assert.equal(projected.install_eligible, false);
});

test('explicit loopback private preview remains preview-only and cannot become an install', () => {
  const preview = record();
  Object.assign(preview, { schema_version: 'vibapp.browser-preview-record.experimental-v1', document_type: 'browser-preview-record-projection',
    publication: { state: 'private-candidate' }, verification: { status: 'locally-derived-awaiting-independent-verifier' },
    compatibility: { profiles: ['web-preview'] } });
  const localBinding = binding();
  localBinding.profile = 'web-preview';
  localBinding.attestation.verification_state = 'locally-derived-awaiting-independent-verifier';
  localBinding.attestation.stage0_activation_eligible = false;
  const args = { previews: [preview], bindings: [localBinding], local: true };
  const app = fixture(args).project([desktopApp()], [cache()])[0];
  assert.equal(app.launch_eligible, true);
  assert.equal(app.install_eligible, false);
  assert.equal(app.web_package_cached, undefined);
  assert.equal(app.verification_state, 'locally-derived-awaiting-independent-verifier');
  assert.equal(fixture({ ...args, hostname: 'vibapp.ai' }).project([desktopApp()], [cache()])[0].launch_eligible, false);
  localBinding.canonical_package_digest_sha256 = '9'.repeat(64);
  assert.equal(fixture(args).project([desktopApp()], [])[0].launch_eligible, false);
});

test('stale private install click is rejected before any parent mutation with a usable explanation', async () => {
  const f = fixture();
  await assert.rejects(f.invoke('submit_install_intent', { payload: { app_id: appId, package_digest_sha256: digest } }), /私有应用请在 VibApp Client 中安装和运行/);
  assert.equal(f.sent.length, 0);
});
