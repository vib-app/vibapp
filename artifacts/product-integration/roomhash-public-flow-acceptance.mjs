#!/usr/bin/env node

/**
 * Build one isolated, synthetic-public Registry + RoomHash locator acceptance fixture.
 *
 * This module deliberately exercises the public filtering/binding path without
 * mutating the production Registry, production locator source, or Website data.
 * The source Builder candidate keeps `authority.publish=none`; this fixture does
 * not grant publication, installation, or execution authority.
 */

import { constants } from 'node:fs';
import {
  lstat,
  mkdir,
  mkdtemp,
  open,
  readFile,
  readdir,
  realpath,
  rm,
  writeFile,
} from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { tmpdir } from 'node:os';
import {
  dirname,
  isAbsolute,
  join,
  relative,
  resolve,
  sep,
} from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  isPublicRegistryRecord,
  syncPublicPackageLocators,
  validatePublicLocator,
} from '../web-client-core/sync-public-package-locators.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const ARTIFACTS = dirname(HERE);
const FIXTURE_SCOPE = 'isolated-synthetic-public-acceptance';
const LOCATOR_SCHEMA = 'vibapp.roomhash-package-locator.experimental-v1';
const TRUST_NOTE = 'locator-only-package-bytes-require-vibapp-verification';
const SUMMARY_SCHEMA = 'vibapp.roomhash-public-flow-acceptance-summary.experimental-v1';
const SHA256 = /^[0-9a-f]{64}$/;
const SAFE_PATH = /^(?!\/)(?!.*(?:^|\/)\.{1,2}(?:\/|$))(?!.*\/\/)[A-Za-z0-9._/-]{1,240}$/;
const MAX_TRANSPORT_BYTES = 256 * 1024;
const MAX_CANDIDATE_BYTES = 1024 * 1024;
const MAX_MANIFEST_BYTES = 1024 * 1024;
const MAX_PACKAGE_BYTES = 64 * 1024 * 1024;

export const DEFAULT_CANDIDATE_PATH = join(
  ARTIFACTS,
  'app-builder',
  'demo-output',
  'pipeline',
  'candidates',
  'dfad1fed5fe0eb8c5bf83927eee64695287e22b5c8149d878b231d69d056e674',
  'candidate.json',
);

const FORBIDDEN_OUTPUT_ROOTS = [
  join(ARTIFACTS, 'product-platform', 'registry', 'snapshots'),
  join(ARTIFACTS, 'product-platform', 'registry', 'package-locators'),
  join(ARTIFACTS, 'product-platform', 'website', 'public', 'data'),
].map(value => resolve(value));

const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
const object = value => value && typeof value === 'object' && !Array.isArray(value) ? value : null;

export function canonicalJson(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(canonicalJson).join(',') + ']';
  return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonicalJson(value[key])).join(',') + '}';
}

function fail(code, detail) {
  const error = new Error(`${code}: ${detail}`);
  error.code = code;
  throw error;
}

function exactKeys(value, expected, label) {
  const item = object(value);
  const actual = item ? Object.keys(item).sort() : [];
  if (!item || actual.join(',') !== [...expected].sort().join(',')) {
    fail('schema-invalid', `${label} is not an exact object`);
  }
  return item;
}

function requireDigest(value, label) {
  if (typeof value !== 'string' || !SHA256.test(value)) {
    fail('schema-invalid', `${label} is not lowercase SHA-256`);
  }
  return value;
}

function parseJsonStrict(text, label) {
  let position = 0;
  const maximumDepth = 64;

  function whitespace() {
    while (/[\t\n\r ]/.test(text[position] || '')) position += 1;
  }

  function string() {
    const start = position;
    if (text[position] !== '"') fail('json-invalid', `${label} contains an invalid string`);
    position += 1;
    while (position < text.length) {
      const character = text[position];
      if (character === '"') {
        position += 1;
        try { return JSON.parse(text.slice(start, position)); } catch {
          fail('json-invalid', `${label} contains an invalid string`);
        }
      }
      if (character === '\\') {
        position += 1;
        if (position >= text.length) break;
        if (text[position] === 'u') {
          if (!/^[0-9a-fA-F]{4}$/.test(text.slice(position + 1, position + 5))) break;
          position += 5;
          continue;
        }
        if (!/["\\/bfnrt]/.test(text[position])) break;
      } else if (character.charCodeAt(0) < 0x20) {
        break;
      }
      position += 1;
    }
    fail('json-invalid', `${label} contains an unterminated or invalid string`);
  }

  function value(depth) {
    if (depth > maximumDepth) fail('resource-limit', `${label} exceeds JSON depth ${maximumDepth}`);
    whitespace();
    const character = text[position];
    if (character === '"') return string();
    if (character === '{') {
      position += 1;
      whitespace();
      const result = Object.create(null);
      const keys = new Set();
      if (text[position] === '}') { position += 1; return result; }
      while (position < text.length) {
        whitespace();
        const key = string();
        if (keys.has(key)) fail('json-invalid', `${label} contains duplicate JSON key ${key}`);
        keys.add(key);
        whitespace();
        if (text[position] !== ':') fail('json-invalid', `${label} is missing an object colon`);
        position += 1;
        result[key] = value(depth + 1);
        whitespace();
        if (text[position] === '}') { position += 1; return result; }
        if (text[position] !== ',') fail('json-invalid', `${label} is missing an object comma`);
        position += 1;
      }
      fail('json-invalid', `${label} contains an unterminated object`);
    }
    if (character === '[') {
      position += 1;
      whitespace();
      const result = [];
      if (text[position] === ']') { position += 1; return result; }
      while (position < text.length) {
        result.push(value(depth + 1));
        whitespace();
        if (text[position] === ']') { position += 1; return result; }
        if (text[position] !== ',') fail('json-invalid', `${label} is missing an array comma`);
        position += 1;
      }
      fail('json-invalid', `${label} contains an unterminated array`);
    }
    for (const [token, parsed] of [['true', true], ['false', false], ['null', null]]) {
      if (text.startsWith(token, position)) { position += token.length; return parsed; }
    }
    const match = /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/.exec(text.slice(position));
    if (!match) fail('json-invalid', `${label} contains an invalid JSON value`);
    position += match[0].length;
    const parsed = Number(match[0]);
    if (!Number.isFinite(parsed) || (Number.isInteger(parsed) && !Number.isSafeInteger(parsed))) {
      fail('json-invalid', `${label} contains a lossy JSON number`);
    }
    return parsed;
  }

  const result = value(0);
  whitespace();
  if (position !== text.length) fail('json-invalid', `${label} contains trailing JSON data`);
  return result;
}

async function readRegular(path, maximumBytes, label) {
  const before = await lstat(path).catch(error => {
    if (error?.code === 'ENOENT') fail('not-found', `${label} is missing`);
    throw error;
  });
  if (!before.isFile() || before.isSymbolicLink() || before.nlink !== 1) {
    fail('integrity-failure', `${label} must be one regular unlinked file`);
  }
  if (before.size < 2 || before.size > maximumBytes) {
    fail('resource-limit', `${label} size is outside 2..${maximumBytes}`);
  }
  const handle = await open(path, constants.O_RDONLY | (constants.O_NOFOLLOW || 0));
  try {
    const opened = await handle.stat();
    if (
      !opened.isFile()
      || opened.dev !== before.dev
      || opened.ino !== before.ino
      || opened.size !== before.size
    ) fail('integrity-failure', `${label} changed while opening`);
    const bytes = await handle.readFile();
    const after = await handle.stat();
    if (
      bytes.length !== before.size
      || after.dev !== opened.dev
      || after.ino !== opened.ino
      || after.size !== opened.size
      || after.mtimeMs !== opened.mtimeMs
    ) fail('integrity-failure', `${label} changed while reading`);
    return bytes;
  } finally {
    await handle.close();
  }
}

async function readJson(path, maximumBytes, label) {
  const bytes = await readRegular(path, maximumBytes, label);
  let text;
  try { text = bytes.toString('utf8'); } catch { fail('json-invalid', `${label} is not UTF-8`); }
  return { bytes, value: parseJsonStrict(text, label) };
}

function containsPath(parent, child) {
  const result = relative(parent, child);
  return result === '' || (result !== '..' && !result.startsWith(`..${sep}`) && !isAbsolute(result));
}

function assertIsolatedOutput(path) {
  const output = resolve(path);
  for (const forbidden of FORBIDDEN_OUTPUT_ROOTS) {
    if (containsPath(output, forbidden) || containsPath(forbidden, output)) {
      fail('unsafe-output', `output overlaps protected production path ${forbidden}`);
    }
  }
}

async function prepareOutput(outputDirectory) {
  if (outputDirectory === null || outputDirectory === undefined) {
    const root = await mkdtemp(join(tmpdir(), 'vibapp-roomhash-public-acceptance-'));
    assertIsolatedOutput(root);
    return realpath(root);
  }
  const requested = resolve(outputDirectory);
  assertIsolatedOutput(requested);
  const metadata = await lstat(requested).catch(error => error?.code === 'ENOENT' ? null : Promise.reject(error));
  if (metadata) {
    if (!metadata.isDirectory() || metadata.isSymbolicLink()) {
      fail('unsafe-output', 'output must be a regular directory');
    }
    if ((await readdir(requested)).length !== 0) fail('unsafe-output', 'output directory must be empty');
  } else {
    await mkdir(requested, { recursive: true, mode: 0o700 });
  }
  const root = await realpath(requested);
  assertIsolatedOutput(root);
  return root;
}

function manifestArtifactPaths(manifest) {
  const artifacts = object(manifest.artifacts);
  if (!artifacts) fail('candidate-invalid', 'manifest artifacts are missing');
  const descriptors = [
    artifacts.canonical_component,
    ...(Array.isArray(artifacts.assets) ? artifacts.assets : []),
    artifacts.provenance,
    artifacts.sbom,
  ];
  for (const derivation of Array.isArray(artifacts.browser_derivations) ? artifacts.browser_derivations : []) {
    descriptors.push(...(Array.isArray(derivation.files) ? derivation.files : []));
    descriptors.push(derivation.derivation_attestation);
  }
  const paths = descriptors.map((descriptor, index) => {
    const item = object(descriptor);
    if (!item || typeof item.path !== 'string' || !SAFE_PATH.test(item.path)) {
      fail('candidate-invalid', `manifest artifact ${index} has an unsafe path`);
    }
    requireDigest(item.sha256, `manifest artifact ${item.path}`);
    if (!Number.isSafeInteger(item.size_bytes) || item.size_bytes < 1 || item.size_bytes > MAX_PACKAGE_BYTES) {
      fail('candidate-invalid', `manifest artifact ${item.path} has an invalid size`);
    }
    return { path: `package/${item.path}`, sha256: item.sha256, size_bytes: item.size_bytes };
  });
  if (new Set(paths.map(item => item.path)).size !== paths.length) {
    fail('candidate-invalid', 'manifest artifact paths are not unique');
  }
  return paths;
}

function normalizeTransport(value, expectedDigest) {
  let transport = value;
  if (object(transport) && Object.keys(transport).sort().join(',') === 'ok,result') {
    if (transport.ok !== true) fail('transport-invalid', 'RoomHash response is not successful');
    transport = transport.result;
  }
  if (object(transport)?.schema_version === LOCATOR_SCHEMA) {
    try { return validatePublicLocator(transport, expectedDigest); } catch (error) {
      fail('transport-invalid', error.message);
    }
  }
  const receipt = exactKeys(transport, [
    'operation', 'package_digest_sha256', 'info_hash', 'magnet_uri', 'size_bytes', 'files',
  ], 'RoomHash seed receipt');
  if (receipt.operation !== 'seed-package') fail('transport-invalid', 'RoomHash receipt is not seed-package');
  try {
    return validatePublicLocator({
      schema_version: LOCATOR_SCHEMA,
      package_digest_sha256: receipt.package_digest_sha256,
      transport: 'bittorrent-v1',
      info_hash: receipt.info_hash,
      magnet_uri: receipt.magnet_uri,
      size_bytes: receipt.size_bytes,
      files: receipt.files,
      trust_note: TRUST_NOTE,
    }, expectedDigest);
  } catch (error) {
    fail('transport-invalid', error.message);
  }
}

async function loadCandidate(candidatePath, locator) {
  const candidateFile = await readJson(candidatePath, MAX_CANDIDATE_BYTES, 'Builder candidate');
  const candidate = exactKeys(candidateFile.value, [
    'schema_version', 'document_type', 'state', 'job_id', 'source_tree_sha256',
    'package_digest_sha256', 'package_directory', 'component', 'manifest',
    'quarantine_receipt_sha256', 'verification', 'authority',
  ], 'Builder candidate');
  if (
    candidate.schema_version !== 'vibapp.builder-candidate.experimental-v1'
    || candidate.document_type !== 'verifier-promoted-candidate'
    || candidate.state !== 'candidate-ready'
    || candidate.package_directory !== 'package'
    || candidate.verification?.authority !== 'independent-verifier'
    || candidate.authority?.install !== 'daemon'
    || candidate.authority?.publish !== 'none'
    || Object.keys(candidate.authority || {}).sort().join(',') !== 'install,publish'
  ) fail('candidate-invalid', 'candidate authority or verification state is invalid');
  requireDigest(candidate.source_tree_sha256, 'candidate source_tree_sha256');
  requireDigest(candidate.package_digest_sha256, 'candidate package_digest_sha256');
  requireDigest(candidate.manifest?.sha256, 'candidate manifest digest');
  requireDigest(candidate.component?.sha256, 'candidate component digest');
  if (candidate.package_digest_sha256 !== locator.package_digest_sha256) {
    fail('binding-failure', 'candidate and RoomHash package digests differ');
  }

  const candidateRoot = dirname(resolve(candidatePath));
  const manifestPath = join(candidateRoot, 'package', candidate.manifest.path);
  const manifestFile = await readJson(manifestPath, MAX_MANIFEST_BYTES, 'candidate manifest');
  const manifest = manifestFile.value;
  if (
    candidate.manifest.path !== 'manifest.json'
    || manifestFile.bytes.length !== candidate.manifest.size_bytes
    || sha256(manifestFile.bytes) !== candidate.manifest.sha256
  ) fail('binding-failure', 'candidate manifest descriptor does not match its bytes');
  if (
    candidate.component?.path !== manifest.artifacts?.canonical_component?.path
    || candidate.component?.sha256 !== manifest.artifacts?.canonical_component?.sha256
    || candidate.component?.size_bytes !== manifest.artifacts?.canonical_component?.size_bytes
  ) fail('binding-failure', 'candidate component and manifest component descriptors differ');
  if (
    candidate.package_digest_sha256 !== locator.package_digest_sha256
    || manifest.app?.id === undefined
    || manifest.app?.version === undefined
  ) fail('binding-failure', 'candidate package identity is incomplete');

  const expectedInventory = [
    { path: 'candidate.json', sha256: sha256(candidateFile.bytes), size_bytes: candidateFile.bytes.length },
    { path: 'package/manifest.json', sha256: candidate.manifest.sha256, size_bytes: candidate.manifest.size_bytes },
    ...manifestArtifactPaths(manifest),
  ].sort((left, right) => left.path.localeCompare(right.path));
  const locatorInventory = [...locator.files].sort((left, right) => left.path.localeCompare(right.path));
  if (canonicalJson(expectedInventory) !== canonicalJson(locatorInventory)) {
    fail('binding-failure', 'RoomHash inventory differs from the verified candidate inventory');
  }
  let total = 0;
  for (const expected of expectedInventory) {
    const path = expected.path === 'candidate.json'
      ? resolve(candidatePath)
      : join(candidateRoot, ...expected.path.split('/'));
    const bytes = await readRegular(path, MAX_PACKAGE_BYTES, `candidate inventory ${expected.path}`);
    if (bytes.length !== expected.size_bytes || sha256(bytes) !== expected.sha256) {
      fail('binding-failure', `candidate inventory bytes differ for ${expected.path}`);
    }
    total += bytes.length;
  }
  if (total !== locator.size_bytes) fail('binding-failure', 'RoomHash total bytes differ from candidate bytes');
  return { candidate, candidateFile, manifest, manifestFile, expectedInventory };
}

function artifactEvidence(descriptor) {
  return {
    media_type: descriptor.media_type,
    sha256: descriptor.sha256,
    size_bytes: descriptor.size_bytes,
    visibility: 'public',
  };
}

function makeRegistryRecord(candidateData) {
  const { candidate, candidateFile, manifest } = candidateData;
  const app = manifest.app;
  const profiles = manifest.runtime.profiles.map(item => item.profile);
  const platforms = manifest.runtime.platforms.flatMap(platform => (
    platform.profiles.map(profile => ({ os: platform.os, arch: platform.arch, profile }))
  ));
  const permissions = manifest.capabilities.map(capability => ({
    interface: capability.interface,
    necessity: capability.necessity,
    scope_digest_sha256: sha256(Buffer.from(canonicalJson(capability.scope))),
    summary: `Isolated acceptance projection for ${capability.interface}.`,
  }));
  const record = {
    schema_version: 'vibapp.registry-record.product-v0.0.1',
    document_type: 'registry-record',
    record_id: `registry.acceptance.${app.id}.${candidate.package_digest_sha256.slice(0, 12)}`,
    record_revision: 1,
    app: {
      id: app.id,
      version: app.version,
      kind: app.kind,
      display_name: app.display_name,
      summary: app.description,
      publisher: {
        publisher_id: app.publisher.id,
        display_name: app.publisher.display_name,
        verification_state: 'verified',
      },
    },
    package: {
      app_id: app.id,
      version: app.version,
      package_digest_sha256: candidate.package_digest_sha256,
      manifest_digest_sha256: candidate.manifest.sha256,
    },
    contract: {
      package_format: manifest.package_format,
      component_contract: manifest.runtime.contract,
      wasi: manifest.runtime.wasi,
      wit_world: manifest.runtime.world,
    },
    compatibility: { platforms, profiles },
    permissions,
    source: {
      visibility: 'public',
      source_digest_sha256: candidate.source_tree_sha256,
      license_spdx: manifest.license.spdx_expression,
    },
    verification: {
      status: 'verified',
      revocation: 'not-revoked',
      evidence: [{
        evidence_id: `acceptance.${candidate.job_id}.candidate`,
        kind: 'verifier-report',
        sha256: sha256(candidateFile.bytes),
      }],
      provenance: artifactEvidence(manifest.artifacts.provenance),
      sbom: artifactEvidence(manifest.artifacts.sbom),
      scan: {
        media_type: 'application/json',
        sha256: sha256(Buffer.from(canonicalJson(candidate.verification))),
        size_bytes: Buffer.byteLength(canonicalJson(candidate.verification)),
        visibility: 'public',
      },
    },
    publication: {
      state: 'published',
      published_at_utc: candidate.verification.verified_at_utc,
      revoked_at_utc: null,
      public_metadata_digest_sha256: '',
    },
    search_metadata: {
      tags: ['isolated', 'acceptance', 'roomhash'],
      capability_labels: permissions.map(item => item.interface),
      search_text_digest_sha256: sha256(Buffer.from(canonicalJson({
        display_name: app.display_name,
        description: app.description,
        interfaces: permissions.map(item => item.interface),
      }))),
    },
    created_at_utc: candidate.verification.verified_at_utc,
  };
  record.publication.public_metadata_digest_sha256 = sha256(Buffer.from(canonicalJson({
    fixture_scope: FIXTURE_SCOPE,
    app: record.app,
    package: record.package,
    source: record.source,
    verification: { status: 'verified', revocation: 'not-revoked' },
  })));
  if (!isPublicRegistryRecord(record)) fail('record-invalid', 'generated Registry record is not public-filter eligible');
  return record;
}

async function writeJson(path, value) {
  await writeFile(path, `${JSON.stringify(value, null, 2)}\n`, { encoding: 'utf8', mode: 0o600, flag: 'wx' });
}

async function selfValidate({ root, candidateData, record, registryPath, sourceDirectory, targetDirectory, index }) {
  const snapshotBytes = await readFile(registryPath);
  const snapshot = parseJsonStrict(snapshotBytes.toString('utf8'), 'generated Registry snapshot');
  const indexBytes = await readFile(join(targetDirectory, 'index.json'));
  const storedIndex = parseJsonStrict(indexBytes.toString('utf8'), 'generated locator index');
  if (canonicalJson(storedIndex) !== canonicalJson(index)) fail('self-check-failed', 'returned and stored indexes differ');
  if (
    Object.keys(snapshot).sort().join(',') !== [
      'browser_artifact_bindings', 'browser_preview_records', 'consumer_notes',
      'corpus_version', 'generated_at_utc', 'platform_status', 'records',
      'sample_search', 'snapshot_version',
    ].sort().join(',')
    || snapshot.platform_status !== FIXTURE_SCOPE
    || snapshot.consumer_notes?.website_projection_mode !== 'production-public-only'
    || snapshot.consumer_notes?.local_private_preview_enabled !== false
    || snapshot.records?.length !== 1
  ) fail('self-check-failed', 'isolated snapshot marker or exact production shape is missing');
  if (index.entries?.length !== 1) fail('self-check-failed', 'locator index does not contain exactly one entry');
  const entry = index.entries[0];
  const normalizedLocatorPath = join(targetDirectory, `${candidateData.candidate.package_digest_sha256}.json`);
  const normalizedLocatorBytes = await readFile(normalizedLocatorPath);
  const normalizedLocator = validatePublicLocator(
    parseJsonStrict(normalizedLocatorBytes.toString('utf8'), 'normalized locator'),
    candidateData.candidate.package_digest_sha256,
  );
  const checks = {
    candidate_authority_unchanged: candidateData.candidate.authority.install === 'daemon'
      && candidateData.candidate.authority.publish === 'none',
    candidate_manifest_digest_bound: record.package.manifest_digest_sha256 === candidateData.candidate.manifest.sha256
      && candidateData.candidate.manifest.sha256 === sha256(candidateData.manifestFile.bytes),
    candidate_package_digest_bound: record.package.package_digest_sha256 === candidateData.candidate.package_digest_sha256
      && normalizedLocator.package_digest_sha256 === candidateData.candidate.package_digest_sha256
      && entry.package_digest_sha256 === candidateData.candidate.package_digest_sha256,
    candidate_source_digest_bound: record.source.source_digest_sha256 === candidateData.candidate.source_tree_sha256,
    registry_snapshot_bound_to_index: index.registry_snapshot_sha256 === sha256(snapshotBytes),
    canonical_record_bound_to_index: entry.registry_record_sha256 === sha256(Buffer.from(canonicalJson(record))),
    normalized_locator_bound_to_index: entry.locator_sha256 === sha256(normalizedLocatorBytes),
    app_identity_bound: entry.app_id === record.app.id && record.package.app_id === record.app.id,
    locator_inventory_bound: canonicalJson([...normalizedLocator.files].sort((left, right) => left.path.localeCompare(right.path)))
      === canonicalJson([...candidateData.expectedInventory].sort((left, right) => left.path.localeCompare(right.path))),
    source_sidecar_is_isolated: containsPath(root, sourceDirectory) && containsPath(root, targetDirectory),
  };
  const failed = Object.entries(checks).filter(([, passed]) => !passed).map(([name]) => name);
  if (failed.length) fail('self-check-failed', `binding checks failed: ${failed.join(', ')}`);
  return {
    checks: Object.keys(checks),
    snapshotBytes,
    normalizedLocatorBytes,
    normalizedLocator,
    entry,
  };
}

export async function createIsolatedPublicAcceptanceFixture({
  transportPath,
  candidatePath = DEFAULT_CANDIDATE_PATH,
  outputDirectory = null,
} = {}) {
  if (typeof transportPath !== 'string' || transportPath.length === 0) {
    fail('invalid-argument', 'transportPath is required');
  }
  const resolvedTransport = resolve(transportPath);
  const resolvedCandidate = resolve(candidatePath);
  const transportFile = await readJson(resolvedTransport, MAX_TRANSPORT_BYTES, 'RoomHash transport receipt');

  // Read the candidate once to obtain its package identity before strict transport normalization.
  const candidateIdentityFile = await readJson(resolvedCandidate, MAX_CANDIDATE_BYTES, 'Builder candidate');
  const expectedDigest = requireDigest(candidateIdentityFile.value?.package_digest_sha256, 'candidate package digest');
  const locator = normalizeTransport(transportFile.value, expectedDigest);
  const candidateData = await loadCandidate(resolvedCandidate, locator);
  const candidateBeforeSha256 = sha256(candidateData.candidateFile.bytes);
  const record = makeRegistryRecord(candidateData);
  const root = await prepareOutput(outputDirectory);
  const publicDataDirectory = join(root, 'public-data');
  const sourceDirectory = join(root, 'package-locator-source');
  const targetDirectory = join(publicDataDirectory, 'package-locators');
  const registryPath = join(publicDataDirectory, 'registry.snapshot.json');
  await mkdir(publicDataDirectory, { recursive: true, mode: 0o700 });
  await mkdir(sourceDirectory, { recursive: true, mode: 0o700 });
  const snapshot = {
    browser_artifact_bindings: [],
    browser_preview_records: [],
    consumer_notes: {
      website_projection_mode: 'production-public-only',
      local_private_preview_enabled: false,
      install_authority: false,
      publication_authority: false,
      acceptance_scope: FIXTURE_SCOPE,
    },
    corpus_version: 'isolated-synthetic-public-acceptance.1',
    platform_status: FIXTURE_SCOPE,
    generated_at_utc: candidateData.candidate.verification.verified_at_utc,
    records: [record],
    sample_search: {
      fixture_scope: FIXTURE_SCOPE,
      matched: [],
      note: 'Synthetic public-filter acceptance only; no publication authority.',
    },
    snapshot_version: 'isolated-synthetic-public-acceptance.1',
  };
  await writeJson(registryPath, snapshot);
  await writeJson(join(sourceDirectory, `${expectedDigest}.json`), locator);
  await writeFile(join(sourceDirectory, 'README.md'), [
    '# Isolated synthetic public acceptance locator source',
    '',
    `Scope: **${FIXTURE_SCOPE}**.`,
    '',
    'This directory exists only to exercise public Registry/locator binding. The source',
    'Builder candidate still has `authority.publish=none`. Nothing here grants production',
    'publication, installation, execution, or Website-data mutation authority.',
    '',
  ].join('\n'), { encoding: 'utf8', mode: 0o600, flag: 'wx' });

  const registryBytes = await readFile(registryPath);
  const index = await syncPublicPackageLocators({
    registryBytes,
    sourceDirectory,
    targetDirectory,
  });
  const verified = await selfValidate({
    root,
    candidateData,
    record,
    registryPath,
    sourceDirectory,
    targetDirectory,
    index,
  });
  const candidateAfterBytes = await readRegular(resolvedCandidate, MAX_CANDIDATE_BYTES, 'Builder candidate after generation');
  if (sha256(candidateAfterBytes) !== candidateBeforeSha256) {
    fail('self-check-failed', 'source Builder candidate changed during generation');
  }

  const summary = {
    schema_version: SUMMARY_SCHEMA,
    state: 'validated-isolated-fixture',
    fixture_scope: FIXTURE_SCOPE,
    authority: {
      source_candidate_publish: 'none',
      production_publication: false,
      production_registry_mutation: false,
      production_website_data_mutation: false,
      installation: false,
      execution: false,
    },
    output_root: root,
    inputs: {
      candidate_path: resolvedCandidate,
      transport_path: resolvedTransport,
      transport_kind: transportFile.value?.schema_version === LOCATOR_SCHEMA ? 'locator' : 'seed-receipt',
    },
    identity: {
      app_id: record.app.id,
      version: record.app.version,
      package_digest_sha256: candidateData.candidate.package_digest_sha256,
      manifest_digest_sha256: candidateData.candidate.manifest.sha256,
      source_digest_sha256: candidateData.candidate.source_tree_sha256,
      candidate_file_sha256: candidateBeforeSha256,
      info_hash: verified.normalizedLocator.info_hash,
    },
    bindings: {
      registry_snapshot_sha256: sha256(verified.snapshotBytes),
      canonical_registry_record_sha256: sha256(Buffer.from(canonicalJson(record))),
      normalized_locator_sha256: sha256(verified.normalizedLocatorBytes),
    },
    paths: {
      public_data_root: publicDataDirectory,
      registry_snapshot: registryPath,
      locator_source: join(sourceDirectory, `${expectedDigest}.json`),
      locator_index: join(targetDirectory, 'index.json'),
      normalized_locator: join(targetDirectory, `${expectedDigest}.json`),
    },
    checks: verified.checks,
  };
  await writeJson(join(root, 'summary.json'), summary);
  return summary;
}

function parseArguments(argv) {
  const result = { transportPath: null, candidatePath: DEFAULT_CANDIDATE_PATH, outputDirectory: null };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === '--transport' || argument === '--transport-json' || argument === '--receipt') {
      result.transportPath = argv[++index];
    } else if (argument === '--candidate') {
      result.candidatePath = argv[++index];
    } else if (argument === '--output') {
      result.outputDirectory = argv[++index];
    } else if (!argument.startsWith('-') && result.transportPath === null) {
      result.transportPath = argument;
    } else {
      fail('invalid-argument', `unknown argument ${argument}`);
    }
  }
  return result;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const summary = await createIsolatedPublicAcceptanceFixture(parseArguments(process.argv.slice(2)));
    process.stdout.write(`${JSON.stringify(summary, null, 2)}\n`);
  } catch (error) {
    process.stderr.write(`${JSON.stringify({
      schema_version: SUMMARY_SCHEMA,
      state: 'rejected',
      code: error?.code || 'internal',
      detail: error?.message || String(error),
    })}\n`);
    process.exitCode = 1;
  }
}
