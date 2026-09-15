import { createHash } from 'node:crypto';
import { lstat, mkdir, mkdtemp, readFile, readdir, realpath, rename, rm, writeFile } from 'node:fs/promises';
import { basename, dirname, isAbsolute, join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const ARTIFACTS = dirname(HERE);
const DEFAULT_REGISTRY = join(ARTIFACTS, 'product-platform', 'registry', 'snapshots', 'registry.snapshot.json');
const DEFAULT_SOURCE = join(ARTIFACTS, 'product-platform', 'registry', 'package-locators');
const DEFAULT_TARGET = join(ARTIFACTS, 'product-platform', 'website', 'public', 'data', 'package-locators');
const LOCATOR_SCHEMA = 'vibapp.roomhash-package-locator.experimental-v1';
const INDEX_SCHEMA = 'vibapp.public-package-locator-index.experimental-v1';
const REGISTRY_RECORD_SCHEMA = 'vibapp.registry-record.product-v0.0.1';
const TRUST_NOTE = 'locator-only-package-bytes-require-vibapp-verification';
const SHA256 = /^[0-9a-f]{64}$/;
const INFO_HASH = /^[0-9a-f]{40}$/;
const APP_ID = /^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$/;
const RECORD_ID = /^[A-Za-z0-9._-]{1,256}$/;
const SAFE_PATH = /^(?!\/)(?!.*(?:^|\/)\.{1,2}(?:\/|$))(?!.*\/\/)[A-Za-z0-9._/-]{1,240}$/;
const MAX_LOCATORS = 1024;
const MAX_LOCATOR_BYTES = 256 * 1024;
const MAX_PACKAGE_BYTES = 64 * 1024 * 1024;
const FIXED_TRACKERS = new Set([
  'wss://tracker.webtorrent.dev',
  'wss://tracker.openwebtorrent.com',
  'wss://tracker.btorrent.xyz',
]);

const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
const object = value => value && typeof value === 'object' && !Array.isArray(value) ? value : null;
const compareUtf8 = (left, right) => Buffer.from(left).compare(Buffer.from(right));

function boundedText(value, maximum) {
  return typeof value === 'string'
    && value.length > 0
    && [...value].length <= maximum
    && ![...value].some(character => /[\u0000-\u001f\u007f]/u.test(character));
}

function hasExactKeys(value, expected) {
  const item = object(value);
  return item !== null
    && Object.keys(item).sort().join(',') === [...expected].sort().join(',');
}

function uniqueBoundedStrings(values, maximumItems, maximumCharacters) {
  return Array.isArray(values)
    && values.length <= maximumItems
    && values.every(value => boundedText(value, maximumCharacters))
    && new Set(values).size === values.length;
}

function validArtifactEvidence(value) {
  return hasExactKeys(value, ['media_type', 'sha256', 'size_bytes', 'visibility'])
    && value.visibility === 'public'
    && boundedText(value.media_type, 128)
    && SHA256.test(value.sha256)
    && Number.isSafeInteger(value.size_bytes)
    && value.size_bytes >= 1
    && value.size_bytes <= MAX_PACKAGE_BYTES;
}

function canonicalJson(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(canonicalJson).join(',') + ']';
  return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonicalJson(value[key])).join(',') + '}';
}

function exactKeys(value, expected, label) {
  const item = object(value);
  if (!item || Object.keys(item).sort().join(',') !== [...expected].sort().join(',')) {
    throw new Error(`${label} is not an exact object`);
  }
  return item;
}

// These namespaces are reserved for the checked-in synthetic conformance corpus.
// Its plausible hashes and "verified" labels are test inputs, never publication.
export function isSyntheticRegistryRecord(record) {
  const prefixed = (value, prefix) => typeof value === 'string' && value.startsWith(prefix);
  return record?.app?.publisher?.publisher_id === 'fixture.vibapp'
    || prefixed(record?.app?.id, 'ai.vibapp.fixture.')
    || prefixed(record?.record_id, 'registry.fixture.')
    || (Array.isArray(record?.verification?.evidence)
      && record.verification.evidence.some(entry => prefixed(entry?.evidence_id, 'fixture.verify.')));
}

export function isPublicRegistryRecord(record) {
  if (isSyntheticRegistryRecord(record)) return false;
  if (!hasExactKeys(record, [
    'app', 'compatibility', 'contract', 'created_at_utc', 'document_type', 'package',
    'permissions', 'publication', 'record_id', 'record_revision', 'schema_version',
    'search_metadata', 'source', 'verification',
  ])) return false;
  if (!hasExactKeys(record.app, ['display_name', 'id', 'kind', 'publisher', 'summary', 'version'])
    || !hasExactKeys(record.app.publisher, ['display_name', 'publisher_id', 'verification_state'])
    || !hasExactKeys(record.compatibility, ['platforms', 'profiles'])
    || !hasExactKeys(record.contract, ['component_contract', 'package_format', 'wasi', 'wit_world'])
    || !hasExactKeys(record.package, ['app_id', 'manifest_digest_sha256', 'package_digest_sha256', 'version'])
    || !hasExactKeys(record.publication, ['public_metadata_digest_sha256', 'published_at_utc', 'revoked_at_utc', 'state'])
    || !hasExactKeys(record.search_metadata, ['capability_labels', 'search_text_digest_sha256', 'tags'])
    || !hasExactKeys(record.source, ['github_archive', 'license_spdx', 'source_digest_sha256', 'visibility'])
    || !hasExactKeys(record.verification, ['evidence', 'provenance', 'revocation', 'sbom', 'scan', 'status'])) {
    return false;
  }
  if (!uniqueBoundedStrings(record.compatibility.profiles, 4, 32)
    || !Array.isArray(record.compatibility.platforms)
    || record.compatibility.platforms.length < 1
    || record.compatibility.platforms.length > 16
    || record.compatibility.platforms.some(platform => (
      !hasExactKeys(platform, ['arch', 'os', 'profile'])
      || !['macos', 'windows', 'linux', 'browser'].includes(platform.os)
      || !['aarch64', 'x86-64', 'wasm32'].includes(platform.arch)
      || !['desktop', 'headless', 'web-preview', 'web-runtime'].includes(platform.profile)
      || !record.compatibility.profiles.includes(platform.profile)
    ))) return false;
  if (!Array.isArray(record.permissions)
    || record.permissions.length > 64
    || record.permissions.some(permission => (
      !hasExactKeys(permission, ['interface', 'necessity', 'scope_digest_sha256', 'summary'])
      || !boundedText(permission.interface, 256)
      || !permission.interface.startsWith('vibapp:experimental-v0/')
      || !permission.interface.endsWith('@0.0.1')
      || !['required', 'degradable'].includes(permission.necessity)
      || !SHA256.test(permission.scope_digest_sha256)
      || !boundedText(permission.summary, 1_000)
    ))
    || new Set(record.permissions.map(permission => permission.interface)).size !== record.permissions.length) {
    return false;
  }
  if (!Array.isArray(record.verification.evidence)
    || record.verification.evidence.length < 1
    || record.verification.evidence.length > 64
    || record.verification.evidence.some(evidence => (
      !hasExactKeys(evidence, ['evidence_id', 'kind', 'sha256'])
      || !boundedText(evidence.evidence_id, 256)
      || !boundedText(evidence.kind, 64)
      || !SHA256.test(evidence.sha256)
    ))) return false;
  return record.schema_version === REGISTRY_RECORD_SCHEMA
    && record.document_type === 'registry-record'
    && RECORD_ID.test(record.record_id || '')
    && Number.isSafeInteger(record.record_revision)
    && record.record_revision >= 1
    && boundedText(record.created_at_utc, 64)
    && record.publication?.state === 'published'
    && SHA256.test(record.publication?.public_metadata_digest_sha256 || '')
    && boundedText(record.publication?.published_at_utc, 64)
    && record.publication?.revoked_at_utc === null
    && record.verification?.status === 'verified'
    && record.verification?.revocation === 'not-revoked'
    && validArtifactEvidence(record.verification.provenance)
    && validArtifactEvidence(record.verification.sbom)
    && validArtifactEvidence(record.verification.scan)
    && ['private', 'public'].includes(record.source?.visibility)
    && hasVerifiedSourceArchive(record)
    && APP_ID.test(record.app?.id || '')
    && record.app.id.length <= 128
    && boundedText(record.app.display_name, 160)
    && boundedText(record.app.summary, 2_000)
    && boundedText(record.app.version, 64)
    && ['ui', 'service', 'hybrid'].includes(record.app.kind)
    && APP_ID.test(record.app.publisher.publisher_id || '')
    && boundedText(record.app.publisher.display_name, 160)
    && record.app?.publisher?.verification_state === 'verified'
    && record.package?.app_id === record.app.id
    && record.package?.version === record.app?.version
    && SHA256.test(record.package?.manifest_digest_sha256 || '')
    && SHA256.test(record.package?.package_digest_sha256 || '')
    && SHA256.test(record.source?.source_digest_sha256 || '')
    && boundedText(record.source.license_spdx, 128)
    && record.contract.package_format === 'vibapp.package.experimental-v0'
    && record.contract.component_contract === 'vibapp:experimental-v0@0.0.1'
    && record.contract.wasi === '0.2'
    && ['ui-only-reference', 'service-only-reference', 'hybrid-reference', 'web-preview-reference'].includes(record.contract.wit_world)
    && uniqueBoundedStrings(record.search_metadata.capability_labels, 64, 128)
    && uniqueBoundedStrings(record.search_metadata.tags, 128, 128)
    && SHA256.test(record.search_metadata.search_text_digest_sha256 || '');
}

export function hasVerifiedSourceArchive(record) {
  const archive = record?.source?.github_archive;
  return hasExactKeys(archive, ['organization', 'repository', 'repository_id', 'commit_sha', 'source_digest_sha256', 'package_digest_sha256'])
    && archive.organization === 'vib-app'
    && (archive.repository === 'sources' && record.source?.visibility === 'public'
      || archive.repository === 'app-' + sha256(Buffer.from(`${record.app?.publisher?.publisher_id}\n${record.app?.id}`, 'utf8')))
    && Number.isSafeInteger(archive.repository_id) && archive.repository_id > 0
    && /^[0-9a-f]{40}$/.test(archive.commit_sha)
    && SHA256.test(archive.source_digest_sha256)
    && archive.source_digest_sha256 === record.source?.source_digest_sha256
    && archive.package_digest_sha256 === record.package?.package_digest_sha256;
}

function validBrowserDescriptor(value, expectedPrefix, { formatRequired = true } = {}) {
  return object(value)
    && typeof value.path === 'string'
    && value.path.startsWith(expectedPrefix)
    && !value.path.split('/').some((part, index) => index > 0 && (part === '' || part === '.' || part === '..'))
    && typeof value.media_type === 'string'
    && (!formatRequired || typeof value.format === 'string')
    && SHA256.test(value.sha256 || '')
    && Number.isSafeInteger(value.size_bytes)
    && value.size_bytes > 0
    && value.size_bytes <= 4 * 1024 * 1024;
}

export function isRealPublicBrowserBinding(record, binding) {
  if (!isPublicRegistryRecord(record) || !object(binding)) return false;
  const componentDigest = String(binding.canonical_component?.sha256 || '');
  const componentPrefix = `/launcher/components/${componentDigest}/`;
  const files = Array.isArray(binding.files) ? binding.files : [];
  return binding.schema_version === 'vibapp.browser-derivation-binding.experimental-v1'
    && binding.source_kind === 'verifier-promoted-candidate'
    && binding.app_id === record.app.id
    && binding.canonical_package_digest_sha256 === record.package.package_digest_sha256
    && (binding.profile === 'web-preview' || binding.profile === 'web-runtime')
    && record.compatibility.profiles.includes(binding.profile)
    && binding.canonical_component?.media_type === 'application/wasm'
    && SHA256.test(componentDigest)
    && Number.isSafeInteger(binding.canonical_component?.size_bytes)
    && binding.canonical_component.size_bytes > 0
    && binding.canonical_component.size_bytes <= 16 * 1024 * 1024
    && binding.derived_from_sha256 === componentDigest
    && binding.entry?.format === 'jco-esm'
    && validBrowserDescriptor(binding.entry, componentPrefix)
    && files.length >= 1
    && files.length <= 32
    && files.every(item => validBrowserDescriptor(item, componentPrefix))
    && new Set(files.map(item => item.path)).size === files.length
    && files.some(item => (
      item.path === binding.entry.path
      && item.sha256 === binding.entry.sha256
      && item.size_bytes === binding.entry.size_bytes
    ))
    && binding.attestation?.verification_state === 'verified'
    && binding.attestation?.canonical_component_transformation_proven === true
    && binding.attestation?.stage0_activation_eligible === true
    && SHA256.test(binding.attestation?.binding_payload_sha256 || '')
    && validBrowserDescriptor(binding.attestation?.artifact, componentPrefix, { formatRequired: false });
}

function validateMagnet(value, digest, infoHash) {
  if (typeof value !== 'string' || value.length > 4096) throw new Error('locator magnet is invalid');
  let url;
  try { url = new URL(value); } catch { throw new Error('locator magnet is invalid'); }
  if (url.protocol !== 'magnet:' || url.hash || url.username || url.password) throw new Error('locator magnet is invalid');
  const keys = [...url.searchParams.keys()];
  if (keys.some(key => !['xt', 'dn', 'tr'].includes(key))) throw new Error('locator magnet has an unsupported parameter');
  const xt = url.searchParams.getAll('xt');
  const dn = url.searchParams.getAll('dn');
  const trackers = url.searchParams.getAll('tr');
  if (
    xt.length !== 1
    || xt[0] !== `urn:btih:${infoHash}`
    || dn.length !== 1
    || dn[0] !== `${digest}.vibapp-candidate`
    || trackers.length < 1
    || trackers.length > FIXED_TRACKERS.size
    || new Set(trackers).size !== trackers.length
    || trackers.some(tracker => !FIXED_TRACKERS.has(tracker))
  ) throw new Error('locator magnet is not bound to the public package');
  const canonical = `magnet:?xt=urn:btih:${infoHash}&dn=${digest}.vibapp-candidate`
    + trackers.map(tracker => `&tr=${encodeURIComponent(tracker)}`).join('');
  if (value !== canonical) throw new Error('locator magnet is not canonical');
}

export function validatePublicLocator(value, expectedDigest) {
  const locator = exactKeys(value, [
    'schema_version', 'package_digest_sha256', 'transport', 'info_hash',
    'magnet_uri', 'size_bytes', 'files', 'trust_note',
  ], 'package locator');
  if (
    locator.schema_version !== LOCATOR_SCHEMA
    || locator.package_digest_sha256 !== expectedDigest
    || !SHA256.test(locator.package_digest_sha256 || '')
    || locator.transport !== 'bittorrent-v1'
    || !INFO_HASH.test(locator.info_hash || '')
    || locator.trust_note !== TRUST_NOTE
    || !Number.isSafeInteger(locator.size_bytes)
    || locator.size_bytes < 2
    || locator.size_bytes > MAX_PACKAGE_BYTES
    || !Array.isArray(locator.files)
    || locator.files.length < 2
    || locator.files.length > 128
  ) throw new Error('package locator header is invalid');
  validateMagnet(locator.magnet_uri, expectedDigest, locator.info_hash);
  const paths = new Set();
  let total = 0;
  const files = locator.files.map((value, index) => {
    const file = exactKeys(value, ['path', 'sha256', 'size_bytes'], `package locator file ${index}`);
    if (
      typeof file.path !== 'string'
      || !SAFE_PATH.test(file.path)
      || paths.has(file.path)
      || !SHA256.test(file.sha256 || '')
      || !Number.isSafeInteger(file.size_bytes)
      || file.size_bytes < 1
      || file.size_bytes > MAX_PACKAGE_BYTES
    ) throw new Error('package locator file is invalid');
    paths.add(file.path);
    total += file.size_bytes;
    if (!Number.isSafeInteger(total) || total > MAX_PACKAGE_BYTES) throw new Error('package locator is too large');
    return { path: file.path, sha256: file.sha256, size_bytes: file.size_bytes };
  });
  if (total !== locator.size_bytes || !paths.has('candidate.json') || !paths.has('package/manifest.json')) {
    throw new Error('package locator inventory is incomplete');
  }
  return {
    schema_version: LOCATOR_SCHEMA,
    package_digest_sha256: expectedDigest,
    transport: 'bittorrent-v1',
    info_hash: locator.info_hash,
    magnet_uri: locator.magnet_uri,
    size_bytes: locator.size_bytes,
    files,
    trust_note: TRUST_NOTE,
  };
}

function hasPublicBrowserRuntime(snapshot, record) {
  return (snapshot.browser_artifact_bindings || [])
    .some(binding => isRealPublicBrowserBinding(record, binding));
}

function validateProductionSnapshot(snapshot) {
  if (!hasExactKeys(snapshot, [
    'browser_artifact_bindings', 'browser_preview_records', 'consumer_notes', 'corpus_version',
    'generated_at_utc', 'platform_status', 'records', 'sample_search', 'snapshot_version',
  ])
    || !Array.isArray(snapshot.records)
    || snapshot.records.length > MAX_LOCATORS
    || !Array.isArray(snapshot.browser_artifact_bindings)
    || snapshot.browser_artifact_bindings.length > MAX_LOCATORS
    || !Array.isArray(snapshot.browser_preview_records)
    || snapshot.browser_preview_records.length !== 0
    || !object(snapshot.consumer_notes)
    || snapshot.consumer_notes.website_projection_mode !== 'production-public-only'
    || snapshot.consumer_notes.local_private_preview_enabled !== false
    || !object(snapshot.sample_search)
    || !boundedText(snapshot.corpus_version, 256)
    || !boundedText(snapshot.generated_at_utc, 64)
    || !boundedText(snapshot.platform_status, 128)
    || !boundedText(snapshot.snapshot_version, 256)) {
    throw new Error('Registry snapshot is not an exact production-public-only projection');
  }
  const appIds = new Set();
  const recordIds = new Set();
  const packageDigests = new Set();
  for (const record of snapshot.records) {
    if (!isPublicRegistryRecord(record)) {
      throw new Error('Registry snapshot contains a record outside the public trust policy');
    }
    if (appIds.has(record.app.id)
      || recordIds.has(record.record_id)
      || packageDigests.has(record.package.package_digest_sha256)) {
      throw new Error('Registry snapshot contains a duplicate public app, record, or package digest');
    }
    appIds.add(record.app.id);
    recordIds.add(record.record_id);
    packageDigests.add(record.package.package_digest_sha256);
  }
  const recordsByApp = new Map(snapshot.records.map(record => [record.app.id, record]));
  const bindingKeys = new Set();
  for (const binding of snapshot.browser_artifact_bindings) {
    const key = `${binding?.app_id || ''}\0${binding?.profile || ''}`;
    if (bindingKeys.has(key)
      || !isRealPublicBrowserBinding(recordsByApp.get(binding?.app_id), binding)) {
      throw new Error('Registry snapshot contains a duplicate browser binding or one outside the public trust policy');
    }
    bindingKeys.add(key);
  }
}

async function readBoundedRegular(path, maximumBytes, label) {
  const metadata = await lstat(path).catch(() => null);
  if (!metadata?.isFile()
    || metadata.isSymbolicLink()
    || metadata.nlink !== 1
    || metadata.size < 2
    || metadata.size > maximumBytes) {
    throw new Error(`${label} is not a bounded ordinary file`);
  }
  const bytes = await readFile(path);
  const after = await lstat(path).catch(() => null);
  if (!after?.isFile()
    || after.isSymbolicLink()
    || after.nlink !== 1
    || after.dev !== metadata.dev
    || after.ino !== metadata.ino
    || after.size !== metadata.size
    || bytes.length !== metadata.size) {
    throw new Error(`${label} changed while it was read`);
  }
  return bytes;
}

export async function verifyPublicRegistryDataRoot({ targetRoot, productionOnly = true } = {}) {
  if (typeof targetRoot !== 'string' || !isAbsolute(targetRoot) || resolve(targetRoot) !== targetRoot) {
    throw new Error('public Registry data root must be an absolute normalized path');
  }
  const rootMetadata = await lstat(targetRoot).catch(() => null);
  if (!rootMetadata?.isDirectory() || rootMetadata.isSymbolicLink()) {
    throw new Error('public Registry data root must be an ordinary directory');
  }
  const canonicalRoot = await realpath(targetRoot);
  const locatorDirectory = join(targetRoot, 'package-locators');
  const locatorMetadata = await lstat(locatorDirectory).catch(() => null);
  if (!locatorMetadata?.isDirectory() || locatorMetadata.isSymbolicLink()) {
    throw new Error('public Registry locator root must be an ordinary directory');
  }
  const canonicalLocatorDirectory = await realpath(locatorDirectory);
  if (canonicalLocatorDirectory !== canonicalRoot
    && !canonicalLocatorDirectory.startsWith(canonicalRoot + sep)) {
    throw new Error('public Registry locator root escapes the data root');
  }
  const rootEntries = (await readdir(targetRoot)).sort();
  if (rootEntries.join(',') !== 'package-locators,registry.snapshot.json') {
    throw new Error('public Registry data root contains an unexpected entry');
  }

  const snapshotBytes = await readBoundedRegular(
    join(targetRoot, 'registry.snapshot.json'),
    4 * 1024 * 1024,
    'Registry snapshot',
  );
  const snapshot = JSON.parse(snapshotBytes.toString('utf8'));
  if (!object(snapshot) || !Array.isArray(snapshot.records)) {
    throw new Error('Registry snapshot is invalid');
  }
  if (productionOnly) validateProductionSnapshot(snapshot);

  const indexBytes = await readBoundedRegular(join(locatorDirectory, 'index.json'), 2 * 1024 * 1024, 'locator index');
  const index = exactKeys(JSON.parse(indexBytes.toString('utf8')), [
    'schema_version', 'trust_note', 'registry_snapshot_sha256', 'entries',
  ], 'locator index');
  if (index.schema_version !== INDEX_SCHEMA
    || index.trust_note !== TRUST_NOTE
    || index.registry_snapshot_sha256 !== sha256(snapshotBytes)
    || !Array.isArray(index.entries)
    || index.entries.length > MAX_LOCATORS) {
    throw new Error('locator index is not bound to the exact Registry snapshot');
  }

  const recordsByDigest = new Map(
    snapshot.records.filter(isPublicRegistryRecord)
      .map(record => [record.package.package_digest_sha256, record]),
  );
  const identities = {
    app: new Set(), record: new Set(), package: new Set(), locator: new Set(),
  };
  const locatorNames = [];
  let previousDigest = '';
  for (const [position, rawEntry] of index.entries.entries()) {
    const entry = exactKeys(rawEntry, [
      'app_id', 'record_id', 'record_revision', 'registry_record_sha256',
      'package_digest_sha256', 'locator_path', 'locator_sha256', 'size_bytes',
      'web_runtime_available',
    ], `locator index entry ${position}`);
    if (!APP_ID.test(entry.app_id || '')
      || !RECORD_ID.test(entry.record_id || '')
      || !Number.isSafeInteger(entry.record_revision)
      || entry.record_revision < 1
      || !SHA256.test(entry.registry_record_sha256 || '')
      || !SHA256.test(entry.package_digest_sha256 || '')
      || !SHA256.test(entry.locator_sha256 || '')
      || !Number.isSafeInteger(entry.size_bytes)
      || entry.size_bytes < 2
      || entry.size_bytes > MAX_PACKAGE_BYTES
      || typeof entry.web_runtime_available !== 'boolean'
      || entry.locator_path !== `/data/package-locators/${entry.package_digest_sha256}.json`
      || entry.package_digest_sha256 <= previousDigest
      || identities.app.has(entry.app_id)
      || identities.record.has(entry.record_id)
      || identities.package.has(entry.package_digest_sha256)
      || identities.locator.has(entry.locator_path)) {
      throw new Error('locator index entry identity, bounds, or ordering is invalid');
    }
    previousDigest = entry.package_digest_sha256;
    identities.app.add(entry.app_id);
    identities.record.add(entry.record_id);
    identities.package.add(entry.package_digest_sha256);
    identities.locator.add(entry.locator_path);
    const record = recordsByDigest.get(entry.package_digest_sha256);
    if (!record
      || entry.app_id !== record.app.id
      || entry.record_id !== record.record_id
      || entry.record_revision !== record.record_revision
      || entry.registry_record_sha256 !== sha256(Buffer.from(canonicalJson(record)))
      || entry.web_runtime_available !== hasPublicBrowserRuntime(snapshot, record)) {
      throw new Error('locator index is not bound to the canonical public Registry record');
    }
    const name = `${entry.package_digest_sha256}.json`;
    const locatorBytes = await readBoundedRegular(join(locatorDirectory, name), MAX_LOCATOR_BYTES, `locator ${name}`);
    if (sha256(locatorBytes) !== entry.locator_sha256) {
      throw new Error('locator bytes do not match the locator index');
    }
    const locator = validatePublicLocator(JSON.parse(locatorBytes.toString('utf8')), entry.package_digest_sha256);
    if (locator.size_bytes !== entry.size_bytes) {
      throw new Error('locator package size does not match the locator index');
    }
    locatorNames.push(name);
  }
  const expectedLocatorEntries = ['index.json', ...locatorNames].sort();
  const actualLocatorEntries = (await readdir(locatorDirectory)).sort();
  if (actualLocatorEntries.join(',') !== expectedLocatorEntries.join(',')) {
    throw new Error('public Registry locator root contains an unexpected or missing entry');
  }
  return {
    registry_snapshot_sha256: sha256(snapshotBytes),
    locator_index_sha256: sha256(indexBytes),
    locator_count: index.entries.length,
  };
}

export async function syncPublicPackageLocators({
  registryPath = DEFAULT_REGISTRY,
  registryBytes = null,
  sourceDirectory = DEFAULT_SOURCE,
  targetDirectory = DEFAULT_TARGET,
} = {}) {
  const snapshotBytes = registryBytes === null
    ? await readFile(registryPath)
    : Buffer.from(registryBytes);
  if (snapshotBytes.length < 2 || snapshotBytes.length > 4 * 1024 * 1024) {
    throw new Error('Registry snapshot exceeds its size limit');
  }
  const registry = JSON.parse(snapshotBytes.toString('utf8'));
  if (!object(registry) || !Array.isArray(registry.records)) throw new Error('Registry snapshot is invalid');
  const publicByDigest = new Map();
  const publicRecordIds = new Set();
  for (const record of registry.records.filter(isPublicRegistryRecord)) {
    const digest = record.package.package_digest_sha256;
    if (publicByDigest.has(digest)) throw new Error('Registry contains a duplicate public package digest');
    if (publicRecordIds.has(record.record_id)) throw new Error('Registry contains a duplicate public record id');
    publicByDigest.set(digest, record);
    publicRecordIds.add(record.record_id);
  }
  const sourceMetadata = await lstat(sourceDirectory).catch(error => error?.code === 'ENOENT' ? null : Promise.reject(error));
  if (sourceMetadata && (sourceMetadata.isSymbolicLink() || !sourceMetadata.isDirectory())) {
    throw new Error('public locator source must be a regular directory');
  }
  const entries = sourceMetadata ? await readdir(sourceDirectory, { withFileTypes: true }) : [];
  if (entries.length > MAX_LOCATORS) throw new Error('public locator source exceeds its file limit');

  const locators = [];
  for (const entry of entries.sort((left, right) => compareUtf8(left.name, right.name))) {
    if (entry.name === 'README.md') continue;
    if (!entry.isFile() || !/^[0-9a-f]{64}\.json$/.test(entry.name)) {
      throw new Error(`public locator source contains an unsafe entry: ${entry.name}`);
    }
    const digest = basename(entry.name, '.json');
    const record = publicByDigest.get(digest);
    if (!record) throw new Error(`locator ${digest} is not bound to a public verified Registry record`);
    const sourcePath = join(sourceDirectory, entry.name);
    const metadata = await lstat(sourcePath);
    if (metadata.isSymbolicLink() || !metadata.isFile()) {
      throw new Error(`public locator source contains an unsafe entry: ${entry.name}`);
    }
    const bytes = await readFile(sourcePath);
    if (bytes.length < 2 || bytes.length > MAX_LOCATOR_BYTES) throw new Error(`locator ${digest} exceeds its size limit`);
    const normalized = validatePublicLocator(JSON.parse(bytes.toString('utf8')), digest);
    const normalizedBytes = Buffer.from(JSON.stringify(normalized, null, 2) + '\n');
    locators.push({
      digest,
      bytes: normalizedBytes,
      entry: {
        app_id: record.app.id,
        record_id: record.record_id,
        record_revision: record.record_revision,
        registry_record_sha256: sha256(Buffer.from(canonicalJson(record))),
        package_digest_sha256: digest,
        locator_path: `/data/package-locators/${digest}.json`,
        locator_sha256: sha256(normalizedBytes),
        size_bytes: normalized.size_bytes,
        web_runtime_available: hasPublicBrowserRuntime(registry, record),
      },
    });
  }

  const parent = dirname(targetDirectory);
  await mkdir(parent, { recursive: true });
  const staging = await mkdtemp(join(parent, '.package-locators.tmp.'));
  try {
    for (const locator of locators) await writeFile(join(staging, `${locator.digest}.json`), locator.bytes, { mode: 0o600 });
    const index = {
      schema_version: INDEX_SCHEMA,
      trust_note: TRUST_NOTE,
      registry_snapshot_sha256: sha256(snapshotBytes),
      entries: locators.map(locator => locator.entry),
    };
    await writeFile(join(staging, 'index.json'), JSON.stringify(index, null, 2) + '\n', { mode: 0o600 });
    const targetMetadata = await lstat(targetDirectory).catch(error => error?.code === 'ENOENT' ? null : Promise.reject(error));
    if (targetMetadata && (targetMetadata.isSymbolicLink() || !targetMetadata.isDirectory())) {
      throw new Error('public locator target must be a regular directory');
    }
    const previous = `${staging}.previous`;
    const targetExists = targetMetadata !== null;
    if (targetExists) await rename(targetDirectory, previous);
    try {
      await rename(staging, targetDirectory);
      await rm(previous, { recursive: true, force: true });
    } catch (error) {
      if (targetExists) await rename(previous, targetDirectory).catch(() => {});
      throw error;
    }
    return index;
  } finally {
    await rm(staging, { recursive: true, force: true });
  }
}

export async function syncPublicRegistryDataRoot({
  registryPath = DEFAULT_REGISTRY,
  registryBytes = null,
  sourceDirectory = DEFAULT_SOURCE,
  targetRoot,
  productionOnly = false,
} = {}) {
  if (typeof targetRoot !== 'string' || !isAbsolute(targetRoot) || resolve(targetRoot) !== targetRoot) {
    throw new Error('public Registry data target must be an absolute normalized path');
  }
  const parent = dirname(targetRoot);
  if (parent === targetRoot) throw new Error('public Registry data target cannot be a filesystem root');
  await mkdir(parent, { recursive: true });
  const parentMetadata = await lstat(parent);
  if (parentMetadata.isSymbolicLink() || !parentMetadata.isDirectory()) {
    throw new Error('public Registry data parent must be a regular directory');
  }
  const targetMetadata = await lstat(targetRoot).catch(error => (
    error?.code === 'ENOENT' ? null : Promise.reject(error)
  ));
  if (targetMetadata && (targetMetadata.isSymbolicLink() || !targetMetadata.isDirectory())) {
    throw new Error('public Registry data target must be a regular directory');
  }

  const snapshotBytes = registryBytes === null
    ? await readFile(registryPath)
    : Buffer.from(registryBytes);
  const staging = await mkdtemp(join(parent, `.${basename(targetRoot)}.tmp.`));
  const previous = `${staging}.previous`;
  let targetMoved = false;
  try {
    const index = await syncPublicPackageLocators({
      registryBytes: snapshotBytes,
      sourceDirectory,
      targetDirectory: join(staging, 'package-locators'),
    });
    await writeFile(join(staging, 'registry.snapshot.json'), snapshotBytes, { mode: 0o600 });
    await verifyPublicRegistryDataRoot({ targetRoot: staging, productionOnly });

    if (targetMetadata) {
      await rename(targetRoot, previous);
      targetMoved = true;
    }
    try {
      await rename(staging, targetRoot);
    } catch (error) {
      if (targetMoved) await rename(previous, targetRoot).catch(() => {});
      throw error;
    }
    if (targetMoved) await rm(previous, { recursive: true, force: true });
    return index;
  } finally {
    await rm(staging, { recursive: true, force: true });
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const arguments_ = process.argv.slice(2);
  if (arguments_.length === 0) {
    const index = await syncPublicPackageLocators();
    process.stdout.write(`${index.entries.length} public package locator(s) synced\n`);
  } else if (arguments_.length === 2 && arguments_[0] === '--verify-production-data-root') {
    if (!isAbsolute(arguments_[1]) || resolve(arguments_[1]) !== arguments_[1]) {
      throw new Error('production data root must be an absolute normalized path');
    }
    const receipt = await verifyPublicRegistryDataRoot({
      targetRoot: arguments_[1],
      productionOnly: true,
    });
    process.stdout.write(JSON.stringify(receipt) + '\n');
  } else {
    throw new Error('usage: sync-public-package-locators.mjs [--verify-production-data-root ABSOLUTE_PATH]');
  }
}
