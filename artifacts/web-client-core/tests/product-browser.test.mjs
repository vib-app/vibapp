import test from 'node:test';
import assert from 'node:assert/strict';
import { canonicalJson, bindingPayload, sha256Hex, manifestPackageDigest, verifyBrowserDerivationBinding,
  verifyDerivationAttestation, verifyDerivationFileSet, verifyGuestLaunchOutput } from '../runtime-integrity.mjs';
import { foregroundBinding } from '../foreground-session.mjs';
import { wallNow, describeHost, transact } from '../product-host-adapter.mjs';

async function fixture() {
  const component = 'c'.repeat(64), prefix = '/launcher/components/' + component + '/';
  const adapter = new TextEncoder().encode('export function wallNow() {}');
  const adapterHash = await sha256Hex(adapter), adapterName = 'host-adapter-' + adapterHash + '.mjs';
  const entry = new TextEncoder().encode("import * as host from './" + adapterName + "'; export const guest = {};\n");
  const entryHash = await sha256Hex(entry), entryName = 'app-' + entryHash + '.jco.mjs';
  const file = (name, sha256, size_bytes, format) => ({ path: prefix + name, media_type: 'text/javascript', sha256, size_bytes, format });
  const files = [file(entryName, entryHash, entry.length, 'jco-esm'), file(adapterName, adapterHash, adapter.length, 'host-adapter-esm')];
  const derivationFiles = files.map(({ format, ...row }) => ({ ...row, path: 'web-runtime/' + row.path.split('/').at(-1) }));
  const derivation = { policy: 'vibapp.product-browser.stateless-v1', canonical_component_sha256: component, tool: { version: '1.15.4' }, guest_memory: 'fresh-instance-per-call' };
  const derivationBytes = new TextEncoder().encode(canonicalJson(derivation) + '\n');
  const artifact = { path: 'component.wasm', sha256: component, size_bytes: 64, media_type: 'application/wasm' };
  const manifest = { app: { id: 'ai.vibapp.synthetic-clock' }, runtime: { profiles: [{ profile: 'web-runtime', background: 'foreground-only', artifact_role: 'browser-derived' }] },
    entrypoints: [{ id: 'launcher.main', routes: { initial: 'clock' }, profiles: ['web-runtime'] }],
    artifacts: { canonical_component: artifact, assets: [], provenance: { ...artifact, path: 'provenance.json' }, sbom: { ...artifact, path: 'sbom.cdx.json' },
      browser_derivations: [{ profile: 'web-runtime', derived_from_sha256: component, entry: derivationFiles[0], files: derivationFiles,
        derivation_attestation: { path: 'web-runtime/derivation.json', sha256: await sha256Hex(derivationBytes), size_bytes: derivationBytes.length, media_type: 'application/json' } }] } };
  const binding = { schema_version: 'vibapp.browser-derivation-binding.experimental-v1', source_kind: 'verifier-promoted-candidate', app_id: manifest.app.id, profile: 'web-runtime',
    launch_entrypoint: 'launcher.main', initial_route: 'clock', preview_record_id: 'synthetic-record', preview_record_revision: 1,
    canonical_package_digest_sha256: await manifestPackageDigest(manifest), canonical_component: { media_type: 'application/wasm', sha256: component, size_bytes: 64 }, derived_from_sha256: component,
    entry: files[0], files, host_adapter: files[1], host_adapter_sha256: adapterHash,
    attestation: { kind: 'product-verified-jco-derivation', trusted_builder_policy: 'vibapp.product-browser.stateless-v1', verification_state: 'verified',
      canonical_component_transformation_proven: true, stage0_activation_eligible: false, product_activation_eligible: true } };
  binding.attestation.binding_payload_sha256 = await sha256Hex(canonicalJson(bindingPayload(binding)));
  const document = { schema_version: 'vibapp.product-browser-binding-attestation.v1', policy: 'vibapp.product-browser.stateless-v1',
    independent_rederivation: true, canonical_package_digest_sha256: binding.canonical_package_digest_sha256,
    binding_payload_sha256: binding.attestation.binding_payload_sha256, manifest, derivation, stage0_activation_eligible: false };
  const attestation = new TextEncoder().encode(canonicalJson(document) + '\n');
  binding.attestation.artifact = { path: prefix + 'attestation.json', media_type: 'application/json', sha256: await sha256Hex(attestation), size_bytes: attestation.length };
  return { binding, document, attestation, files: new Map([[files[0].path, entry], [files[1].path, adapter]]) };
}

test('product browser activation is separate from historical Stage 0 and bound to complete manifest package digest', async () => {
  const value = await fixture();
  await verifyBrowserDerivationBinding(value.binding);
  await verifyDerivationAttestation(value.binding, value.attestation);
  await verifyDerivationFileSet(value.binding, value.files);
  for (const mutate of [row => row.profile = 'desktop', row => row.launch_entrypoint = 'forged', row => row.attestation.product_activation_eligible = false,
    row => row.attestation.stage0_activation_eligible = true, row => row.canonical_package_digest_sha256 = '9'.repeat(64)]) {
    const bad = structuredClone(value.binding); mutate(bad);
    await assert.rejects(verifyBrowserDerivationBinding(bad));
  }
  value.document.manifest.app.id = 'ai.vibapp.other';
  const changed = new TextEncoder().encode(canonicalJson(value.document) + '\n');
  value.binding.attestation.artifact.sha256 = await sha256Hex(changed);
  value.binding.attestation.artifact.size_bytes = changed.length;
  await assert.rejects(verifyDerivationAttestation(value.binding, changed), /product-manifest-package-digest/);
});

test('browser artifact tampering is rejected before executable import', async () => {
  const value = await fixture();
  value.files.set(value.binding.entry.path, new TextEncoder().encode('malicious replacement'));
  await assert.rejects(verifyDerivationFileSet(value.binding, value.files), /size-mismatch|digest-mismatch/);
});

test('launcher.main and initial clock route are retained in foreground binding', async () => {
  const { binding } = await fixture();
  const request = { binding, app_id: binding.app_id, session: 'test-session-1234567890', package_digest_sha256: binding.canonical_package_digest_sha256, component_sha256: binding.derived_from_sha256 };
  const descriptor = { id: binding.app_id, version: '1.0.0', displayName: 'Clock', kind: 'ui', entrypoints: [{ id: 'launcher.main', kind: 'launcher-ui' }] };
  const surface = { session: request.session, surface: 'surface-main', route: 'clock', view: { title: 'Clock', root: 'root', nodes: [{ id: 'root', kind: { tag: 'text', val: { text: '12:00:00' } } }] } };
  verifyGuestLaunchOutput(request, descriptor, { surfaces: [surface] });
  assert.equal(foregroundBinding(request, surface).entrypoint, 'launcher.main');
  assert.equal(foregroundBinding(request, surface).route, 'clock');
});

test('real foreground wall clock has canonical UTC and no persistent write authority', () => {
  const clock = wallNow();
  assert.match(clock.nowUtc, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/);
  assert.ok(Math.abs(Date.parse(clock.nowUtc) - Date.now()) < 1000);
  assert.equal(describeHost().profile, 'web-runtime');
  assert.equal(describeHost().background, 'foreground-only');
  assert.throws(() => transact({ operations: [] }), error => error.code === 'capability-unavailable');
});
