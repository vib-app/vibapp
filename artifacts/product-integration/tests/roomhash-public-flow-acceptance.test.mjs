import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { chmod, cp, mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import test from 'node:test';

import {
  DEFAULT_CANDIDATE_PATH,
  canonicalJson,
  createIsolatedPublicAcceptanceFixture,
} from '../roomhash-public-flow-acceptance.mjs';

const DIGEST = 'dfad1fed5fe0eb8c5bf83927eee64695287e22b5c8149d878b231d69d056e674';
const INFO_HASH = '7fff835f7a255cd9501c90c06c6678e8353bcd32';
const TRACKERS = [
  'wss://tracker.webtorrent.dev',
  'wss://tracker.openwebtorrent.com',
  'wss://tracker.btorrent.xyz',
];
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');

async function inventory(candidatePath = DEFAULT_CANDIDATE_PATH) {
  const root = dirname(candidatePath);
  const candidateBytes = await readFile(candidatePath);
  const candidate = JSON.parse(candidateBytes);
  const manifestBytes = await readFile(join(root, 'package', candidate.manifest.path));
  const manifest = JSON.parse(manifestBytes);
  const descriptors = [
    manifest.artifacts.canonical_component,
    ...manifest.artifacts.assets,
    manifest.artifacts.provenance,
    manifest.artifacts.sbom,
  ];
  const files = [{ path: 'candidate.json', sha256: sha256(candidateBytes), size_bytes: candidateBytes.length }];
  files.push({ path: 'package/manifest.json', sha256: sha256(manifestBytes), size_bytes: manifestBytes.length });
  for (const descriptor of descriptors) {
    const bytes = await readFile(join(root, 'package', descriptor.path));
    files.push({ path: `package/${descriptor.path}`, sha256: sha256(bytes), size_bytes: bytes.length });
  }
  return files;
}

async function transportFixture(root, { envelope = false, locator = false, candidatePath = DEFAULT_CANDIDATE_PATH } = {}) {
  const files = await inventory(candidatePath);
  const sizeBytes = files.reduce((total, item) => total + item.size_bytes, 0);
  const magnet = `magnet:?xt=urn:btih:${INFO_HASH}&dn=${DIGEST}.vibapp-candidate`
    + TRACKERS.map(tracker => `&tr=${encodeURIComponent(tracker)}`).join('');
  const receipt = locator ? {
    schema_version: 'vibapp.roomhash-package-locator.experimental-v1',
    package_digest_sha256: DIGEST,
    transport: 'bittorrent-v1',
    info_hash: INFO_HASH,
    magnet_uri: magnet,
    size_bytes: sizeBytes,
    files,
    trust_note: 'locator-only-package-bytes-require-vibapp-verification',
  } : {
    operation: 'seed-package',
    package_digest_sha256: DIGEST,
    info_hash: INFO_HASH,
    magnet_uri: magnet,
    size_bytes: sizeBytes,
    files,
  };
  const path = join(root, locator ? 'locator.json' : 'seed-receipt.json');
  await writeFile(path, JSON.stringify(envelope ? { ok: true, result: receipt } : receipt));
  return path;
}

test('isolated fixture binds the real verified Builder candidate through all three digest layers', async t => {
  const scratch = await mkdtemp(join(tmpdir(), 'vibapp-public-flow-positive-'));
  t.after(() => rm(scratch, { recursive: true, force: true }));
  const transportPath = await transportFixture(scratch, { envelope: true });
  const output = join(scratch, 'acceptance-output');
  const candidateBefore = await readFile(DEFAULT_CANDIDATE_PATH);
  const summary = await createIsolatedPublicAcceptanceFixture({ transportPath, outputDirectory: output });
  const candidateAfter = await readFile(DEFAULT_CANDIDATE_PATH);

  assert.equal(summary.state, 'validated-isolated-fixture');
  assert.equal(summary.fixture_scope, 'isolated-synthetic-public-acceptance');
  assert.equal(summary.authority.source_candidate_publish, 'none');
  assert.equal(summary.authority.production_publication, false);
  assert.deepEqual(candidateAfter, candidateBefore);
  assert.equal(summary.identity.package_digest_sha256, DIGEST);
  assert.equal(summary.checks.length, 10);

  const snapshotBytes = await readFile(summary.paths.registry_snapshot);
  const snapshot = JSON.parse(snapshotBytes);
  const index = JSON.parse(await readFile(summary.paths.locator_index));
  const locatorBytes = await readFile(summary.paths.normalized_locator);
  assert.equal(snapshot.platform_status, 'isolated-synthetic-public-acceptance');
  assert.deepEqual(Object.keys(snapshot).sort(), [
    'browser_artifact_bindings', 'browser_preview_records', 'consumer_notes',
    'corpus_version', 'generated_at_utc', 'platform_status', 'records',
    'sample_search', 'snapshot_version',
  ].sort());
  assert.equal(snapshot.consumer_notes.website_projection_mode, 'production-public-only');
  assert.equal(snapshot.consumer_notes.local_private_preview_enabled, false);
  assert.equal(snapshot.records[0].package.package_digest_sha256, DIGEST);
  assert.equal(snapshot.records[0].source.source_digest_sha256, summary.identity.source_digest_sha256);
  assert.equal(index.registry_snapshot_sha256, sha256(snapshotBytes));
  assert.equal(index.entries[0].registry_record_sha256, sha256(Buffer.from(canonicalJson(snapshot.records[0]))));
  assert.equal(index.entries[0].locator_sha256, sha256(locatorBytes));
  assert.deepEqual((await readdir(output)).sort(), [
    'package-locator-source', 'public-data', 'summary.json',
  ]);
  assert.equal(summary.paths.public_data_root, join(summary.output_root, 'public-data'));
});

test('canonical locator input is accepted and normalized through syncPublicPackageLocators', async t => {
  const scratch = await mkdtemp(join(tmpdir(), 'vibapp-public-flow-locator-'));
  t.after(() => rm(scratch, { recursive: true, force: true }));
  const transportPath = await transportFixture(scratch, { locator: true });
  const summary = await createIsolatedPublicAcceptanceFixture({ transportPath });
  t.after(() => rm(summary.output_root, { recursive: true, force: true }));
  assert.equal(summary.inputs.transport_kind, 'locator');
  assert.equal(summary.identity.info_hash, INFO_HASH);
  assert.equal(JSON.parse(await readFile(summary.paths.normalized_locator)).trust_note,
    'locator-only-package-bytes-require-vibapp-verification');
});

test('digest mismatch and duplicate JSON keys fail before output is created', async t => {
  const scratch = await mkdtemp(join(tmpdir(), 'vibapp-public-flow-negative-'));
  t.after(() => rm(scratch, { recursive: true, force: true }));
  const transportPath = await transportFixture(scratch);
  const receipt = JSON.parse(await readFile(transportPath));
  receipt.package_digest_sha256 = 'a'.repeat(64);
  await writeFile(transportPath, JSON.stringify(receipt));
  const output = join(scratch, 'must-not-exist');
  await assert.rejects(
    () => createIsolatedPublicAcceptanceFixture({ transportPath, outputDirectory: output }),
    /transport-invalid|binding-failure/,
  );
  await assert.rejects(() => readdir(output), error => error?.code === 'ENOENT');

  await writeFile(transportPath, '{"operation":"seed-package","operation":"seed-package"}');
  await assert.rejects(
    () => createIsolatedPublicAcceptanceFixture({ transportPath, outputDirectory: output }),
    /duplicate JSON key/,
  );
});

test('candidate publish authority cannot be widened for acceptance', async t => {
  const scratch = await mkdtemp(join(tmpdir(), 'vibapp-public-flow-authority-'));
  t.after(() => rm(scratch, { recursive: true, force: true }));
  const copiedCandidateRoot = join(scratch, 'candidate');
  await cp(dirname(DEFAULT_CANDIDATE_PATH), copiedCandidateRoot, { recursive: true });
  const copiedCandidatePath = join(copiedCandidateRoot, 'candidate.json');
  await chmod(copiedCandidatePath, 0o600);
  const candidate = JSON.parse(await readFile(copiedCandidatePath));
  candidate.authority.publish = 'public';
  await writeFile(copiedCandidatePath, JSON.stringify(candidate));
  const transportPath = await transportFixture(scratch, { candidatePath: copiedCandidatePath });
  await assert.rejects(
    () => createIsolatedPublicAcceptanceFixture({
      transportPath,
      candidatePath: copiedCandidatePath,
      outputDirectory: join(scratch, 'output'),
    }),
    /candidate authority or verification state is invalid/,
  );
});

test('production Registry and Website data paths are hard output boundaries', async t => {
  const scratch = await mkdtemp(join(tmpdir(), 'vibapp-public-flow-output-'));
  t.after(() => rm(scratch, { recursive: true, force: true }));
  const transportPath = await transportFixture(scratch);
  for (const outputDirectory of [
    join(process.cwd(), 'artifacts/product-platform/registry/snapshots/acceptance'),
    join(process.cwd(), 'artifacts/product-platform/registry/package-locators/acceptance'),
    join(process.cwd(), 'artifacts/product-platform/website/public/data/acceptance'),
  ]) {
    await assert.rejects(
      () => createIsolatedPublicAcceptanceFixture({ transportPath, outputDirectory }),
      /unsafe-output/,
    );
  }
});
