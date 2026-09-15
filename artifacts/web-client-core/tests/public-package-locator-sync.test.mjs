import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { buildRegistryProjection } from '../sync-web-gui.mjs';

import {
  isPublicRegistryRecord,
  isRealPublicBrowserBinding,
  syncPublicPackageLocators,
  syncPublicRegistryDataRoot,
  validatePublicLocator,
  verifyPublicRegistryDataRoot,
} from '../sync-public-package-locators.mjs';

const DIGEST = 'a'.repeat(64);
const PRIVATE_DIGEST = 'b'.repeat(64);
const INFO_HASH = 'c'.repeat(40);
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');

function canonicalJson(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(canonicalJson).join(',') + ']';
  return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonicalJson(value[key])).join(',') + '}';
}

function record(digest = DIGEST, publication = 'published') {
  return {
    schema_version: 'vibapp.registry-record.product-v0.0.1',
    document_type: 'registry-record',
    record_id: `registry.${digest.slice(0, 8)}.1`,
    record_revision: 1,
    app: {
      id: `ai.vibapp.example.${digest.slice(0, 8)}`,
      version: '1.0.0',
      kind: 'ui',
      display_name: 'Public Fixture',
      summary: 'A bounded public Registry fixture.',
      publisher: {
        publisher_id: 'publisher.example',
        display_name: 'VibApp Fixtures',
        verification_state: 'verified',
      },
    },
    compatibility: {
      profiles: ['web-preview'],
      platforms: [{ os: 'browser', arch: 'wasm32', profile: 'web-preview' }],
    },
    contract: {
      package_format: 'vibapp.package.experimental-v0',
      component_contract: 'vibapp:experimental-v0@0.0.1',
      wasi: '0.2',
      wit_world: 'ui-only-reference',
    },
    created_at_utc: '2026-08-28T00:00:00Z',
    package: {
      app_id: `ai.vibapp.example.${digest.slice(0, 8)}`,
      version: '1.0.0',
      manifest_digest_sha256: '9'.repeat(64),
      package_digest_sha256: digest,
    },
    publication: {
      state: publication,
      public_metadata_digest_sha256: '8'.repeat(64),
      published_at_utc: '2026-08-28T00:00:00Z',
      revoked_at_utc: null,
    },
    permissions: [{
      interface: 'vibapp:experimental-v0/kv@0.0.1',
      necessity: 'required',
      scope_digest_sha256: '4'.repeat(64),
      summary: 'App-scoped storage.',
    }],
    search_metadata: {
      capability_labels: ['local-state'],
      search_text_digest_sha256: '3'.repeat(64),
      tags: ['fixture'],
    },
    verification: {
      status: 'verified',
      revocation: 'not-revoked',
      evidence: [{ evidence_id: 'example.verify.1', kind: 'verifier-report', sha256: '2'.repeat(64) }],
      provenance: { media_type: 'application/json', sha256: '1'.repeat(64), size_bytes: 1, visibility: 'public' },
      sbom: { media_type: 'application/json', sha256: '0'.repeat(64), size_bytes: 1, visibility: 'public' },
      scan: { media_type: 'application/json', sha256: 'f'.repeat(64), size_bytes: 1, visibility: 'public' },
    },
    source: {
      github_archive: {
        organization: 'vib-app',
        repository: 'app-' + sha256(Buffer.from(`publisher.example\nai.vibapp.example.${digest.slice(0, 8)}`)),
        repository_id: 12345, commit_sha: 'a'.repeat(40),
        source_digest_sha256: '7'.repeat(64), package_digest_sha256: digest,
      },
      visibility: 'public',
      source_digest_sha256: '7'.repeat(64),
      license_spdx: 'Apache-2.0',
    },
  };
}

test('public listing requires a version-bound organization source archive, not public source visibility', () => {
  const value = record();
  value.source.visibility = 'private';
  assert.equal(isPublicRegistryRecord(value), true);
  for (const mutate of [
    v => { delete v.source.github_archive; },
    v => { v.source.github_archive.organization = 'someone-else'; },
    v => { v.source.github_archive.repository = 'app-' + '0'.repeat(64); },
    v => { v.source.github_archive.commit_sha = 'pending'; },
    v => { v.source.github_archive.source_digest_sha256 = '0'.repeat(64); },
    v => { v.source.github_archive.package_digest_sha256 = '0'.repeat(64); },
    v => { v.source.github_archive.repository_id = 0; },
  ]) {
    const invalid = structuredClone(value);
    mutate(invalid);
    assert.equal(isPublicRegistryRecord(invalid), false);
    assert.throws(() => buildRegistryProjection({ records: [invalid] }), /public trust policy/);
  }
});

test('shared sources receipt requires honest public visibility and exact digests', () => {
  const value = record();
  value.source.github_archive.repository = 'sources';
  assert.equal(isPublicRegistryRecord(value), true);
  value.source.visibility = 'private';
  assert.equal(isPublicRegistryRecord(value), false);
  value.source.visibility = 'public';
  value.source.github_archive.package_digest_sha256 = '0'.repeat(64);
  assert.equal(isPublicRegistryRecord(value), false);
});

function browserBinding(boundRecord = record()) {
  const component = '6'.repeat(64);
  const prefix = `/launcher/components/${component}/`;
  const entry = {
    path: `${prefix}entry.js`, media_type: 'text/javascript', format: 'jco-esm',
    sha256: 'd'.repeat(64), size_bytes: 1,
  };
  return {
    schema_version: 'vibapp.browser-derivation-binding.experimental-v1',
    source_kind: 'verifier-promoted-candidate',
    app_id: boundRecord.app.id,
    profile: 'web-preview',
    canonical_package_digest_sha256: boundRecord.package.package_digest_sha256,
    canonical_component: { media_type: 'application/wasm', sha256: component, size_bytes: 1 },
    derived_from_sha256: component,
    entry,
    files: [entry],
    attestation: {
      binding_payload_sha256: '5'.repeat(64),
      verification_state: 'verified',
      canonical_component_transformation_proven: true,
      stage0_activation_eligible: true,
      artifact: {
        path: `${prefix}attestation.json`, media_type: 'application/json',
        sha256: 'e'.repeat(64), size_bytes: 1,
      },
    },
  };
}

test('reserved synthetic corpus cannot acquire public authority from test labels', () => {
  for (const mutate of [
    value => { value.app.publisher.publisher_id = 'fixture.vibapp'; },
    value => { value.app.id = 'ai.vibapp.fixture.todo'; value.package.app_id = value.app.id; },
    value => { value.record_id = 'registry.fixture.todo'; },
    value => { value.verification.evidence[0].evidence_id = 'fixture.verify.1'; },
  ]) {
    const value = record(); mutate(value);
    assert.equal(isPublicRegistryRecord(value), false);
    assert.deepEqual(buildRegistryProjection(productionSnapshot([value])).records, []);
  }
  assert.equal(isPublicRegistryRecord({ app: { id: 42 } }), false);
});

function productionSnapshot(records = [record()], bindings = []) {
  return {
    browser_artifact_bindings: bindings,
    browser_preview_records: [],
    consumer_notes: {
      website_projection_mode: 'production-public-only',
      local_private_preview_enabled: false,
    },
    corpus_version: 'fixture-corpus.1',
    generated_at_utc: '2026-08-28T00:00:00Z',
    platform_status: 'experimental-product-hold',
    records,
    sample_search: {},
    snapshot_version: 'fixture-snapshot.1',
  };
}

function locator({ digest = DIGEST, infoHash = INFO_HASH, tracker = 'wss://tracker.webtorrent.dev' } = {}) {
  return {
    schema_version: 'vibapp.roomhash-package-locator.experimental-v1',
    package_digest_sha256: digest,
    transport: 'bittorrent-v1',
    info_hash: infoHash,
    magnet_uri: `magnet:?xt=urn:btih:${infoHash}&dn=${digest}.vibapp-candidate&tr=${encodeURIComponent(tracker)}`,
    size_bytes: 2,
    files: [
      { path: 'candidate.json', sha256: 'd'.repeat(64), size_bytes: 1 },
      { path: 'package/manifest.json', sha256: 'e'.repeat(64), size_bytes: 1 },
    ],
    trust_note: 'locator-only-package-bytes-require-vibapp-verification',
  };
}

async function fixture() {
  const root = await mkdtemp(join(tmpdir(), 'vibapp-public-locators-'));
  const sourceDirectory = join(root, 'source');
  const targetDirectory = join(root, 'target');
  const registryPath = join(root, 'registry.json');
  await mkdir(sourceDirectory);
  const registryBytes = Buffer.from(JSON.stringify({
    records: [record(), record(PRIVATE_DIGEST, 'private')],
    browser_artifact_bindings: [browserBinding()],
  }) + '\n');
  await writeFile(registryPath, registryBytes);
  return {
    root,
    sourceDirectory,
    targetDirectory,
    registryPath,
    expectedRegistryBytes: registryBytes,
    cleanup: () => rm(root, { recursive: true, force: true }),
  };
}

test('public locator sync publishes only Registry-bound exact sidecars', async t => {
  const files = await fixture();
  t.after(files.cleanup);
  const empty = await syncPublicPackageLocators(files);
  assert.deepEqual(empty.entries, []);
  assert.equal(empty.registry_snapshot_sha256, sha256(files.expectedRegistryBytes));
  await writeFile(join(files.sourceDirectory, `${DIGEST}.json`), JSON.stringify(locator()));
  const index = await syncPublicPackageLocators(files);
  assert.equal(index.entries.length, 1);
  assert.equal(index.entries[0].app_id, record().app.id);
  assert.equal(index.entries[0].package_digest_sha256, DIGEST);
  assert.equal(index.entries[0].registry_record_sha256, sha256(Buffer.from(canonicalJson(record()))));
  assert.equal(index.entries[0].web_runtime_available, true);
  assert.match(index.entries[0].locator_sha256, /^[0-9a-f]{64}$/);
  assert.deepEqual(
    JSON.parse(await readFile(join(files.targetDirectory, `${DIGEST}.json`), 'utf8')),
    validatePublicLocator(locator(), DIGEST),
  );
});

test('public locator sync rejects private records and preserves the prior target', async t => {
  const files = await fixture();
  t.after(files.cleanup);
  await writeFile(join(files.sourceDirectory, `${DIGEST}.json`), JSON.stringify(locator()));
  await syncPublicPackageLocators(files);
  const previous = await readFile(join(files.targetDirectory, 'index.json'), 'utf8');
  const previousLocator = await readFile(join(files.targetDirectory, `${DIGEST}.json`));
  await writeFile(
    join(files.sourceDirectory, `${PRIVATE_DIGEST}.json`),
    JSON.stringify(locator({ digest: PRIVATE_DIGEST })),
  );
  await assert.rejects(() => syncPublicPackageLocators(files), /not bound to a public verified Registry record/);
  assert.equal(await readFile(join(files.targetDirectory, 'index.json'), 'utf8'), previous);
  assert.deepEqual(await readFile(join(files.targetDirectory, `${DIGEST}.json`)), previousLocator);
});

test('empty production snapshot is reproducible and removes every stale locator', async t => {
  const files = await fixture();
  t.after(files.cleanup);
  const first = await syncPublicPackageLocators(files);
  const firstBytes = await readFile(join(files.targetDirectory, 'index.json'));
  await writeFile(join(files.targetDirectory, `${PRIVATE_DIGEST}.json`), 'stale-private-locator\n');
  const second = await syncPublicPackageLocators(files);
  const secondBytes = await readFile(join(files.targetDirectory, 'index.json'));
  assert.deepEqual(second, first);
  assert.deepEqual(secondBytes, firstBytes);
  assert.deepEqual(await readdir(files.targetDirectory), ['index.json']);
});

test('complete public Registry data root switches snapshot and locators as one generation', async t => {
  const files = await fixture();
  t.after(files.cleanup);
  await writeFile(join(files.sourceDirectory, `${DIGEST}.json`), JSON.stringify(locator()));

  const first = await syncPublicRegistryDataRoot({
    registryPath: files.registryPath,
    sourceDirectory: files.sourceDirectory,
    targetRoot: files.targetDirectory,
  });
  const firstSnapshot = await readFile(join(files.targetDirectory, 'registry.snapshot.json'));
  const firstIndex = await readFile(join(files.targetDirectory, 'package-locators', 'index.json'));
  assert.deepEqual(firstSnapshot, files.expectedRegistryBytes);
  assert.equal(first.registry_snapshot_sha256, sha256(firstSnapshot));
  assert.equal(JSON.parse(firstIndex).registry_snapshot_sha256, sha256(firstSnapshot));

  await writeFile(join(files.targetDirectory, 'stale-private-data.json'), 'must be removed\n');
  await syncPublicRegistryDataRoot({
    registryBytes: files.expectedRegistryBytes,
    sourceDirectory: files.sourceDirectory,
    targetRoot: files.targetDirectory,
  });
  assert.deepEqual(await readdir(files.targetDirectory), ['package-locators', 'registry.snapshot.json']);

  const acceptedSnapshot = await readFile(join(files.targetDirectory, 'registry.snapshot.json'));
  const acceptedIndex = await readFile(join(files.targetDirectory, 'package-locators', 'index.json'));
  await writeFile(
    join(files.sourceDirectory, `${PRIVATE_DIGEST}.json`),
    JSON.stringify(locator({ digest: PRIVATE_DIGEST })),
  );
  await assert.rejects(
    () => syncPublicRegistryDataRoot({
      registryBytes: files.expectedRegistryBytes,
      sourceDirectory: files.sourceDirectory,
      targetRoot: files.targetDirectory,
    }),
    /not bound to a public verified Registry record/,
  );
  assert.deepEqual(
    await readFile(join(files.targetDirectory, 'registry.snapshot.json')),
    acceptedSnapshot,
  );
  assert.deepEqual(
    await readFile(join(files.targetDirectory, 'package-locators', 'index.json')),
    acceptedIndex,
  );
});

test('public locator validation rejects arbitrary trackers and extra authority fields', () => {
  assert.equal(isPublicRegistryRecord(record()), true);
  assert.equal(isRealPublicBrowserBinding(record(), browserBinding()), true);
  assert.equal(isPublicRegistryRecord(record(PRIVATE_DIGEST, 'private')), false);
  assert.throws(
    () => validatePublicLocator(locator({ tracker: 'wss://attacker.invalid' }), DIGEST),
    /not bound to the public package/,
  );
  assert.throws(
    () => validatePublicLocator({ ...locator(), install_authority: true }, DIGEST),
    /not an exact object/,
  );
  const missingTracker = locator();
  missingTracker.magnet_uri = `magnet:?xt=urn:btih:${INFO_HASH}&dn=${DIGEST}.vibapp-candidate`;
  assert.throws(() => validatePublicLocator(missingTracker, DIGEST), /not bound to the public package/);
  const nonCanonical = locator();
  nonCanonical.magnet_uri = `magnet:?dn=${DIGEST}.vibapp-candidate&xt=urn:btih:${INFO_HASH}&tr=${encodeURIComponent('wss://tracker.webtorrent.dev')}`;
  assert.throws(() => validatePublicLocator(nonCanonical, DIGEST), /not canonical/);
});

test('public record trust fields and version binding all fail closed in projection and locator sync', () => {
  const cases = [
    ['schema', value => { value.schema_version = 'vibapp.registry-record.unknown'; }],
    ['record-id', value => { value.record_id = '../unsafe'; }],
    ['record-revision', value => { value.record_revision = 0; }],
    ['publisher', value => { value.app.publisher.verification_state = 'unverified'; }],
    ['metadata-digest', value => { value.publication.public_metadata_digest_sha256 = 'BAD'; }],
    ['revocation-time', value => { value.publication.revoked_at_utc = '2026-08-28T01:00:00Z'; }],
    ['manifest-digest', value => { value.package.manifest_digest_sha256 = 'BAD'; }],
    ['source-digest', value => { value.source.source_digest_sha256 = 'BAD'; }],
    ['version', value => { value.package.version = '2.0.0'; }],
  ];
  for (const [label, mutate] of cases) {
    const invalid = structuredClone(record());
    mutate(invalid);
    assert.equal(isPublicRegistryRecord(invalid), false, label);
    assert.throws(
      () => buildRegistryProjection(productionSnapshot([invalid])),
      /claims public authority but fails the exact public trust policy/,
      label,
    );
  }
});

test('production data verifier binds snapshot, index, canonical record, and runtime availability', async t => {
  const files = await fixture();
  t.after(files.cleanup);
  await writeFile(join(files.sourceDirectory, `${DIGEST}.json`), JSON.stringify(locator()));
  const snapshotBytes = Buffer.from(JSON.stringify(productionSnapshot([record()], [browserBinding()])) + '\n');
  await syncPublicRegistryDataRoot({
    registryBytes: snapshotBytes,
    sourceDirectory: files.sourceDirectory,
    targetRoot: files.targetDirectory,
    productionOnly: true,
  });
  const receipt = await verifyPublicRegistryDataRoot({ targetRoot: files.targetDirectory });
  assert.equal(receipt.registry_snapshot_sha256, sha256(snapshotBytes));
  assert.equal(receipt.locator_count, 1);

  const indexPath = join(files.targetDirectory, 'package-locators', 'index.json');
  const index = JSON.parse(await readFile(indexPath, 'utf8'));
  index.entries[0].web_runtime_available = false;
  await writeFile(indexPath, JSON.stringify(index) + '\n');
  await assert.rejects(
    () => verifyPublicRegistryDataRoot({ targetRoot: files.targetDirectory }),
    /canonical public Registry record/,
  );
});

test('Registry binding rejects records missing exact public verification metadata', async t => {
  const files = await fixture();
  t.after(files.cleanup);
  const incomplete = record();
  delete incomplete.package.manifest_digest_sha256;
  await writeFile(files.registryPath, JSON.stringify({ records: [incomplete], browser_artifact_bindings: [] }));
  await writeFile(join(files.sourceDirectory, `${DIGEST}.json`), JSON.stringify(locator()));
  assert.equal(isPublicRegistryRecord(incomplete), false);
  await assert.rejects(
    () => syncPublicPackageLocators(files),
    /not bound to a public verified Registry record/,
  );
});

test('Website dev and production build entrypoints include locator publication', async () => {
  const syncSource = await readFile(new URL('../sync-web-gui.mjs', import.meta.url), 'utf8');
  const locatorSyncSource = await readFile(new URL('../sync-public-package-locators.mjs', import.meta.url), 'utf8');
  const websitePackage = JSON.parse(await readFile(
    new URL('../../product-platform/website/package.json', import.meta.url),
    'utf8',
  ));
  const wrapper = await readFile(new URL('../../product-platform/website/scripts/sync-gui.mjs', import.meta.url), 'utf8');
  assert.match(syncSource, /syncPublicRegistryDataRoot,/);
  assert.match(syncSource, /export async function syncProductionPublicRegistryData\(\)/);
  assert.match(syncSource, /targetRoot: stableProductionPublicDataRoot/);
  assert.match(syncSource, /targetRoot: join\(websiteRoot, 'public', 'data'\)/);
  assert.match(locatorSyncSource, /writeFile\(join\(staging, 'registry\.snapshot\.json'\), snapshotBytes/);
  assert.match(locatorSyncSource, /targetDirectory: join\(staging, 'package-locators'\)/);
  for (const hook of ['predev', 'prebuild']) {
    assert.equal(websitePackage.scripts[hook], 'node scripts/sync-gui.mjs');
  }
  assert.equal(websitePackage.scripts['prebuild:local'], 'node ../../web-client-core/sync-web-gui.mjs');
  assert.match(wrapper, /import\('\.\.\/\.\.\/\.\.\/web-client-core\/sync-web-gui\.mjs'\)/);
  assert.match(wrapper, /await syncWebGui\(\)/);
  assert.match(wrapper, /Prepared GUI input changed/);
});

test('public locator schemas require snapshot and record binding and the fixed tracker allowlist', async () => {
  const indexSchema = JSON.parse(await readFile(
    new URL('../../product-platform/registry/schemas/public-package-locator-index.experimental-v1.schema.json', import.meta.url),
    'utf8',
  ));
  const locatorSchema = JSON.parse(await readFile(
    new URL('../../product-platform/registry/schemas/roomhash-package-locator.experimental-v1.schema.json', import.meta.url),
    'utf8',
  ));
  assert.equal(indexSchema.additionalProperties, false);
  assert.ok(indexSchema.required.includes('registry_snapshot_sha256'));
  assert.ok(indexSchema.properties.entries.items.required.includes('registry_record_sha256'));
  const magnetPattern = new RegExp(locatorSchema.properties.magnet_uri.pattern);
  assert.equal(magnetPattern.test(locator().magnet_uri), true);
  assert.equal(magnetPattern.test(locator({ tracker: 'wss://attacker.invalid' }).magnet_uri), false);
  assert.equal(locatorSchema.properties.files.allOf.length, 2);
});
