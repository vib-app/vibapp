const SHA256 = /^[0-9a-f]{64}$/;
const INFO_HASH = /^[0-9a-f]{40}$/i;
const CANONICAL_INFO_HASH = /^[0-9a-f]{40}$/;
const MAX_PACKAGE_BYTES = 64 * 1024 * 1024;
const MAX_PACKAGE_FILES = 128;
const MAX_IN_FLIGHT_BYTES = 128 * 1024 * 1024;
const MAX_TRANSFER_COUNT = 32;
const MAX_TIMEOUT_MS = 120_000;
const MAX_CANDIDATE_DOCUMENT_BYTES = 1024 * 1024;
const SAFE_PACKAGE_PATH = /^(?!\/)(?!.*(?:^|\/)\.{1,2}(?:\/|$))(?!.*\/\/)[A-Za-z0-9._/-]{1,240}$/;
const TRACKERS = Object.freeze([
  'wss://tracker.webtorrent.dev',
  'wss://tracker.openwebtorrent.com',
  'wss://tracker.btorrent.xyz',
]);

function fail(reason) {
  throw new Error('roomhash-browser:' + reason);
}

function record(value, reason) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail(reason);
  return value;
}

function exactKeys(value, allowed, reason) {
  record(value, reason);
  if (Object.keys(value).some(key => !allowed.includes(key))) fail(reason + '-fields');
  return value;
}

function boundedInteger(value, fallback, minimum, maximum, reason) {
  const selected = value === undefined ? fallback : value;
  if (!Number.isSafeInteger(selected) || selected < minimum || selected > maximum) fail(reason);
  return selected;
}

function bytesView(value) {
  if (value instanceof ArrayBuffer) return new Uint8Array(value);
  if (ArrayBuffer.isView(value)) return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  fail('package-bytes-type');
}

function safeRelativePath(value, reason) {
  if (
    typeof value !== 'string'
    || !SAFE_PACKAGE_PATH.test(value)
  ) fail(reason);
  return value;
}

function candidateFileDescriptor(value, index) {
  exactKeys(value, ['path', 'sha256', 'sizeBytes'], 'candidate-file-' + index);
  const path = safeRelativePath(value.path, 'candidate-file-path');
  if (!SHA256.test(value.sha256 || '')) fail('candidate-file-sha256');
  const sizeBytes = boundedInteger(value.sizeBytes, undefined, 1, MAX_PACKAGE_BYTES, 'candidate-file-size');
  return Object.freeze({ path, sha256: value.sha256, sizeBytes });
}

function strictCandidateMagnet(input, packageDigestSha256) {
  if (!CANONICAL_INFO_HASH.test(input.infoHash || '')) fail('candidate-infohash');
  const infoHash = input.infoHash.toLowerCase();
  if (
    typeof input.magnetURI !== 'string'
    || input.magnetURI.length > 4096
    || !input.magnetURI.startsWith('magnet:?')
  ) fail('candidate-magnet');
  let parsed;
  try {
    parsed = new URL(input.magnetURI);
  } catch {
    fail('candidate-magnet');
  }
  if (
    parsed.protocol !== 'magnet:'
    || parsed.pathname !== ''
    || parsed.hostname !== ''
    || parsed.username !== ''
    || parsed.password !== ''
    || parsed.hash !== ''
  ) fail('candidate-magnet');
  const allowed = new Set(['xt', 'dn', 'tr']);
  for (const key of parsed.searchParams.keys()) {
    if (!allowed.has(key)) fail('candidate-magnet-parameter');
  }
  const exactTopics = parsed.searchParams.getAll('xt');
  const displayNames = parsed.searchParams.getAll('dn');
  const trackers = parsed.searchParams.getAll('tr');
  if (
    exactTopics.length !== 1
    || exactTopics[0].toLowerCase() !== 'urn:btih:' + infoHash
    || displayNames.length !== 1
    || displayNames[0] !== packageDigestSha256 + '.vibapp-candidate'
    || trackers.length < 1
    || trackers.length > TRACKERS.length
  ) fail('candidate-magnet-binding');
  const seen = new Set();
  for (const tracker of trackers) {
    if (!TRACKERS.includes(tracker) || !tracker.startsWith('wss://') || seen.has(tracker)) {
      fail('candidate-magnet-tracker');
    }
    seen.add(tracker);
  }
  return Object.freeze({ infoHash, magnetURI: input.magnetURI, trackers: Object.freeze(trackers) });
}

function candidateLocator(input) {
  exactKeys(
    input,
    ['packageDigestSha256', 'infoHash', 'magnetURI', 'files', 'timeoutMs'],
    'candidate-locator',
  );
  if (!SHA256.test(input.packageDigestSha256 || '')) fail('candidate-package-digest');
  const magnet = strictCandidateMagnet(input, input.packageDigestSha256);
  if (!Array.isArray(input.files) || input.files.length < 2 || input.files.length > MAX_PACKAGE_FILES) {
    fail('candidate-file-count');
  }
  const seen = new Set();
  let totalBytes = 0;
  const files = input.files.map((value, index) => {
    const descriptor = candidateFileDescriptor(value, index);
    if (seen.has(descriptor.path)) fail('candidate-file-duplicate');
    seen.add(descriptor.path);
    totalBytes += descriptor.sizeBytes;
    if (!Number.isSafeInteger(totalBytes) || totalBytes > MAX_PACKAGE_BYTES) fail('candidate-size-limit');
    return descriptor;
  });
  if (!seen.has('candidate.json') || !seen.has('package/manifest.json') || totalBytes < 2) {
    fail('candidate-file-layout');
  }
  return Object.freeze({
    packageDigestSha256: input.packageDigestSha256,
    ...magnet,
    files: Object.freeze(files),
    totalBytes,
  });
}

function documentDescriptor(value, reason) {
  record(value, reason);
  const path = safeRelativePath(value.path, reason + '-path');
  if (!SHA256.test(value.sha256 || '')) fail(reason + '-sha256');
  const sizeBytes = boundedInteger(value.size_bytes, undefined, 0, MAX_PACKAGE_BYTES, reason + '-size');
  return { path, sha256: value.sha256, sizeBytes };
}

function manifestFileDescriptors(manifest) {
  const artifacts = record(record(manifest, 'candidate-manifest').artifacts, 'candidate-manifest-artifacts');
  if (!Array.isArray(artifacts.assets) || !Array.isArray(artifacts.browser_derivations)) {
    fail('candidate-manifest-artifacts');
  }
  const values = [artifacts.canonical_component, ...artifacts.assets, artifacts.provenance, artifacts.sbom];
  for (const derivation of artifacts.browser_derivations) {
    if (!Array.isArray(derivation?.files)) fail('candidate-manifest-derivation');
    values.push(...derivation.files, derivation.derivation_attestation);
  }
  return values.map((value, index) => documentDescriptor(value, 'candidate-manifest-file-' + index));
}

function parseJsonBytes(bytes, reason) {
  try {
    return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
  } catch {
    fail(reason);
  }
}

function verifyCandidateDocuments(files, expectedPackageDigest, candidateBytes, manifestBytes) {
  const byPath = new Map(files.map(file => [file.path, file]));
  const candidateFile = byPath.get('candidate.json');
  const manifestFile = byPath.get('package/manifest.json');
  if (!candidateFile || !manifestFile) fail('candidate-file-layout');
  const candidate = record(parseJsonBytes(candidateBytes, 'candidate-json'), 'candidate-document');
  if (
    candidate.schema_version !== 'vibapp.builder-candidate.experimental-v1'
    || candidate.document_type !== 'verifier-promoted-candidate'
    || candidate.state !== 'candidate-ready'
    || candidate.package_directory !== 'package'
    || candidate.package_digest_sha256 !== expectedPackageDigest
    || candidate.authority?.install !== 'daemon'
    || candidate.authority?.publish !== 'none'
    || candidate.verification?.authority !== 'independent-verifier'
    || !Array.isArray(candidate.verification?.checks)
    || candidate.verification.checks.length < 1
    || candidate.verification.checks.some(check => check?.outcome !== 'pass')
  ) fail('candidate-verification-authority');
  const manifestDescriptor = documentDescriptor(candidate.manifest, 'candidate-manifest');
  const componentDescriptor = documentDescriptor(candidate.component, 'candidate-component');
  if (
    manifestDescriptor.path !== 'manifest.json'
    || componentDescriptor.path !== 'component.wasm'
    || manifestDescriptor.sha256 !== manifestFile.sha256
    || manifestDescriptor.sizeBytes !== manifestFile.sizeBytes
  ) fail('candidate-canonical-descriptor');
  const manifest = parseJsonBytes(manifestBytes, 'candidate-manifest-json');
  const descriptors = manifestFileDescriptors(manifest);
  const canonical = descriptors.find(item => item.path === 'component.wasm');
  if (
    !canonical
    || canonical.sha256 !== componentDescriptor.sha256
    || canonical.sizeBytes !== componentDescriptor.sizeBytes
  ) fail('candidate-component-binding');
  const expectedPaths = new Set(['candidate.json', 'package/manifest.json']);
  for (const descriptor of descriptors) {
    const path = 'package/' + descriptor.path;
    if (expectedPaths.has(path)) fail('candidate-descriptor-duplicate');
    expectedPaths.add(path);
    const file = byPath.get(path);
    if (!file || file.sha256 !== descriptor.sha256 || file.sizeBytes !== descriptor.sizeBytes) {
      fail('candidate-descriptor-mismatch');
    }
  }
  if (expectedPaths.size !== byPath.size || [...byPath.keys()].some(path => !expectedPaths.has(path))) {
    fail('candidate-unverified-file');
  }
}

async function verifyCandidateInventory(files, expectedPackageDigest) {
  for (const file of files) {
    if (file.bytes.byteLength !== file.sizeBytes || await sha256Hex(file.bytes) !== file.sha256) {
      fail('candidate-file-integrity');
    }
  }
  const byPath = new Map(files.map(file => [file.path, file]));
  verifyCandidateDocuments(
    files,
    expectedPackageDigest,
    byPath.get('candidate.json')?.bytes,
    byPath.get('package/manifest.json')?.bytes,
  );
}

async function sha256Hex(bytes) {
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
}

function verifiedDescriptor(input, requireLocator) {
  record(input, 'package-input');
  if (!SHA256.test(input.sha256 || '')) fail('package-sha256');
  const sizeBytes = boundedInteger(input.sizeBytes, undefined, 1, MAX_PACKAGE_BYTES, 'package-size');
  if (requireLocator) {
    if (typeof input.magnetURI !== 'string' || input.magnetURI.length > 4096 || !/^magnet:\?xt=urn:btih:[a-z0-9]+(?:&|$)/i.test(input.magnetURI)) {
      fail('package-magnet');
    }
  }
  return { sha256: input.sha256, sizeBytes, magnetURI: input.magnetURI };
}

function normalizedSettings(input = {}) {
  record(input, 'settings');
  if (input.p2pEnabled !== undefined && input.p2pEnabled !== true) fail('p2p-disabled');
  if (input.seedVerifiedApps !== undefined && typeof input.seedVerifiedApps !== 'boolean') fail('seed-setting');
  const maxActiveTransfers = boundedInteger(input.maxActiveTransfers, 4, 1, MAX_TRANSFER_COUNT, 'transfer-limit');
  const uploadKib = boundedInteger(input.uploadLimitKibPerSecond, 0, 0, 1024 * 1024, 'upload-limit');
  const downloadKib = boundedInteger(input.downloadLimitKibPerSecond, 0, 0, 1024 * 1024, 'download-limit');
  return Object.freeze({
    maxActiveTransfers,
    seedVerifiedApps: input.seedVerifiedApps !== false,
    // Product settings expose 0 as "unlimited". WebTorrent uses -1 for unlimited and
    // interprets 0 as an enabled zero-byte throttle, so translate explicitly.
    uploadLimit: uploadKib === 0 ? -1 : uploadKib * 1024,
    downloadLimit: downloadKib === 0 ? -1 : downloadKib * 1024,
  });
}

function transportOptions(settings) {
  return Object.freeze({
    dht: false,
    lsd: false,
    maxConns: Math.min(128, settings.maxActiveTransfers * 4),
    uploadLimit: settings.uploadLimit,
    downloadLimit: settings.downloadLimit,
    tracker: Object.freeze({ announce: TRACKERS }),
  });
}

async function defaultTransportFactory(options) {
  const module = await import('./vendor-roomhash/webtorrent.min.js');
  if (typeof module.default !== 'function') fail('transport-module');
  return new module.default(options);
}

function torrentIdentity(torrent, expectedInfoHash) {
  if (!INFO_HASH.test(torrent?.infoHash || '')) fail('transport-infohash');
  const infoHash = torrent.infoHash.toLowerCase();
  if (expectedInfoHash !== undefined && infoHash !== expectedInfoHash) fail('candidate-infohash-mismatch');
  if (typeof torrent.magnetURI !== 'string' || torrent.magnetURI.length > 4096 || !torrent.magnetURI.startsWith('magnet:')) {
    fail('transport-magnet');
  }
  return { infoHash, magnetURI: torrent.magnetURI };
}

function callWithCallback(target, method, args) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const done = (error, value) => {
      if (settled) return;
      settled = true;
      if (error) reject(error);
      else resolve(value);
    };
    try {
      const returned = target[method](...args, (...callbackArgs) => {
        if (callbackArgs[0] instanceof Error) done(callbackArgs[0]);
        else done(null, callbackArgs.at(-1));
      });
      if (returned && typeof returned.then === 'function') returned.then(value => done(null, value), done);
    } catch (error) {
      done(error);
    }
  });
}

function waitForDone(torrent, timeoutMs) {
  if (torrent?.done === true) return Promise.resolve(torrent);
  if (typeof torrent?.once !== 'function') return Promise.resolve(torrent);
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => finish(new Error('roomhash-browser:fetch-timeout')), timeoutMs);
    const finish = error => {
      clearTimeout(timeout);
      torrent.removeListener?.('done', onDone);
      torrent.removeListener?.('error', onError);
      error ? reject(error) : resolve(torrent);
    };
    const onDone = () => finish();
    const onError = error => finish(error || new Error('roomhash-browser:fetch-failed'));
    torrent.once('done', onDone);
    torrent.once('error', onError);
  });
}

async function fileBytes(file) {
  let value;
  if (typeof file?.arrayBuffer === 'function') value = await file.arrayBuffer();
  else if (typeof file?.blob === 'function') value = await (await file.blob()).arrayBuffer();
  else if (typeof file?.getBuffer === 'function') value = await callWithCallback(file, 'getBuffer', []);
  else fail('transport-file-reader');
  const bytes = bytesView(value);
  return bytes.byteOffset === 0 && bytes.byteLength === bytes.buffer.byteLength ? bytes : bytes.slice();
}

function candidateTorrentFiles(torrent, descriptors) {
  if (!Array.isArray(torrent?.files) || torrent.files.length !== descriptors.length) {
    fail('candidate-file-count-mismatch');
  }
  const candidateMatches = torrent.files.filter(file => (
    file?.path === 'candidate.json'
    || (typeof file?.path === 'string' && file.path.endsWith('/candidate.json'))
  ));
  if (candidateMatches.length !== 1) fail('candidate-root-ambiguous');
  const candidatePath = candidateMatches[0].path;
  const prefix = candidatePath.slice(0, candidatePath.length - 'candidate.json'.length);
  if (prefix && !prefix.endsWith('/')) fail('candidate-root-ambiguous');
  const observed = new Map();
  for (const file of torrent.files) {
    if (typeof file?.path !== 'string' || !file.path.startsWith(prefix)) fail('candidate-root-mismatch');
    const path = safeRelativePath(file.path.slice(prefix.length), 'candidate-downloaded-path');
    if (observed.has(path)) fail('candidate-file-duplicate');
    observed.set(path, file);
  }
  return descriptors.map(descriptor => {
    const file = observed.get(descriptor.path);
    if (!file || file.length !== descriptor.sizeBytes) fail('candidate-file-size-mismatch');
    return Object.freeze({ descriptor, file });
  });
}

async function downloadedCandidateFiles(torrent, descriptors) {
  const files = [];
  for (const { descriptor, file } of candidateTorrentFiles(torrent, descriptors)) {
    const bytes = await fileBytes(file);
    files.push(Object.freeze({ ...descriptor, bytes }));
  }
  return Object.freeze(files);
}

async function stageCandidateFiles(torrent, descriptors, packageDigestSha256, stage) {
  let candidateBytes;
  let manifestBytes;
  for (const { descriptor, file } of candidateTorrentFiles(torrent, descriptors)) {
    const bytes = await fileBytes(file);
    if (bytes.byteLength !== descriptor.sizeBytes || await sha256Hex(bytes) !== descriptor.sha256) {
      fail('candidate-file-integrity');
    }
    if (descriptor.path === 'candidate.json' || descriptor.path === 'package/manifest.json') {
      if (bytes.byteLength > MAX_CANDIDATE_DOCUMENT_BYTES) fail('candidate-document-size');
      if (descriptor.path === 'candidate.json') candidateBytes = bytes;
      else manifestBytes = bytes;
    }
    await stage.writeFile({ ...descriptor, bytes });
  }
  verifyCandidateDocuments(descriptors, packageDigestSha256, candidateBytes, manifestBytes);
}

function cloneCandidateTransfer(value) {
  return Object.freeze({
    ...value,
    files: Object.freeze(value.files.map(file => Object.freeze({ ...file, bytes: file.bytes.slice() }))),
  });
}

function candidateStoreReceipt(value, locator) {
  record(value, 'candidate-store-receipt');
  if (
    value.schemaVersion !== 'vibapp.browser-package-cache-receipt.experimental-v1'
    || value.packageDigestSha256 !== locator.packageDigestSha256
    || value.state !== 'committed'
    || value.fileCount !== locator.files.length
    || value.sizeBytes !== locator.totalBytes
  ) fail('candidate-store-receipt');
  const committedAtUnixMs = boundedInteger(
    value.committedAtUnixMs,
    undefined,
    0,
    Number.MAX_SAFE_INTEGER,
    'candidate-store-receipt-time',
  );
  return Object.freeze({
    schemaVersion: value.schemaVersion,
    packageDigestSha256: value.packageDigestSha256,
    state: value.state,
    fileCount: value.fileCount,
    sizeBytes: value.sizeBytes,
    committedAtUnixMs,
  });
}

function publicStatus(state, client, activeTransfers, inFlightBytes, lastError) {
  return Object.freeze({
    schemaVersion: 'vibapp.roomhash-browser-status.experimental-v1',
    state,
    running: state === 'running',
    foregroundOnly: true,
    implementation: 'roomhash-current-webtorrent-3.0.16',
    activeTransfers,
    inFlightBytes,
    torrents: Array.isArray(client?.torrents) ? client.torrents.length : 0,
    uploadBytesPerSecond: Number.isFinite(client?.uploadSpeed) && client.uploadSpeed >= 0 ? client.uploadSpeed : 0,
    downloadBytesPerSecond: Number.isFinite(client?.downloadSpeed) && client.downloadSpeed >= 0 ? client.downloadSpeed : 0,
    lastError,
  });
}

export function createRoomHashBrowserNode({ transportFactory = defaultTransportFactory, candidateStore = null } = {}) {
  if (typeof transportFactory !== 'function') fail('transport-factory');
  if (candidateStore !== null && (
    typeof candidateStore !== 'object'
    || typeof candidateStore.begin !== 'function'
  )) fail('candidate-store');
  let state = 'stopped';
  let client = null;
  let settings = null;
  let startPromise = null;
  let stopPromise = null;
  let activeTransfers = 0;
  let inFlightBytes = 0;
  let lastError = null;
  const seeds = new Map();
  const fetches = new Map();
  const candidateFetches = new Map();
  const candidateStoreFetches = new Map();

  function status() {
    return publicStatus(state, client, activeTransfers, inFlightBytes, lastError);
  }

  async function start(input = {}) {
    const requested = normalizedSettings(input);
    if (state === 'running') {
      if (JSON.stringify(requested) !== JSON.stringify(settings)) fail('already-started-with-different-settings');
      return status();
    }
    if (state === 'starting') {
      if (JSON.stringify(requested) !== JSON.stringify(settings)) fail('already-started-with-different-settings');
      return startPromise;
    }
    if (state === 'stopping') {
      await stopPromise;
      return start(input);
    }
    state = 'starting';
    lastError = null;
    settings = requested;
    startPromise = (async () => {
      try {
        const created = await transportFactory(transportOptions(requested));
        if (!created || typeof created.seed !== 'function' || typeof created.add !== 'function' || typeof created.destroy !== 'function') {
          fail('transport-shape');
        }
        client = created;
        state = 'running';
        return status();
      } catch (error) {
        client = null;
        settings = null;
        state = 'stopped';
        lastError = error instanceof Error ? error.message.slice(0, 256) : 'transport-start';
        throw error;
      } finally {
        startPromise = null;
      }
    })();
    return startPromise;
  }

  function reserveTransfer(sizeBytes) {
    if (state !== 'running' || !client) fail('not-running');
    if (activeTransfers >= settings.maxActiveTransfers) fail('transfer-limit-reached');
    if (inFlightBytes + sizeBytes > MAX_IN_FLIGHT_BYTES) fail('in-flight-byte-limit-reached');
    activeTransfers += 1;
    inFlightBytes += sizeBytes;
    return client;
  }

  function releaseTransfer(sizeBytes) {
    activeTransfers = Math.max(0, activeTransfers - 1);
    inFlightBytes = Math.max(0, inFlightBytes - sizeBytes);
  }

  async function seedVerifiedPackage(input) {
    record(input, 'package-input');
    const source = bytesView(input.bytes);
    const descriptor = verifiedDescriptor({ ...input, sizeBytes: source.byteLength }, false);
    if (typeof input.name !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$/.test(input.name)) fail('package-name');
    if (!settings?.seedVerifiedApps) fail('verified-seeding-disabled');
    const transport = reserveTransfer(descriptor.sizeBytes);
    const bytes = source.slice();
    let actualDigest;
    try {
      actualDigest = await sha256Hex(bytes);
    } catch (error) {
      releaseTransfer(descriptor.sizeBytes);
      throw error;
    }
    if (actualDigest !== descriptor.sha256) {
      releaseTransfer(descriptor.sizeBytes);
      fail('package-digest-mismatch');
    }
    if (seeds.has(descriptor.sha256)) {
      releaseTransfer(descriptor.sizeBytes);
      return seeds.get(descriptor.sha256);
    }
    if (seeds.size >= MAX_TRANSFER_COUNT) {
      releaseTransfer(descriptor.sizeBytes);
      fail('seed-receipt-limit-reached');
    }
    const operation = (async () => {
      try {
        const torrent = await callWithCallback(transport, 'seed', [bytes, { name: input.name, announce: TRACKERS }]);
        const identity = torrentIdentity(torrent);
        return Object.freeze({
          schemaVersion: 'vibapp.roomhash-package-transfer.experimental-v1',
          operation: 'seed',
          sha256: descriptor.sha256,
          sizeBytes: descriptor.sizeBytes,
          ...identity,
        });
      } finally {
        releaseTransfer(descriptor.sizeBytes);
      }
    })();
    seeds.set(descriptor.sha256, operation);
    try {
      return await operation;
    } catch (error) {
      seeds.delete(descriptor.sha256);
      lastError = error instanceof Error ? error.message.slice(0, 256) : 'seed-failed';
      throw error;
    }
  }

  async function fetchVerifiedPackage(input) {
    const descriptor = verifiedDescriptor(input, true);
    const timeoutMs = boundedInteger(input.timeoutMs, 30_000, 1_000, MAX_TIMEOUT_MS, 'fetch-timeout');
    const key = descriptor.sha256 + ':' + descriptor.sizeBytes + ':' + descriptor.magnetURI;
    if (fetches.has(key)) {
      const result = await fetches.get(key);
      return Object.freeze({ ...result, bytes: result.bytes.slice() });
    }
    const transport = reserveTransfer(descriptor.sizeBytes);
    const operation = (async () => {
      let torrent;
      try {
        torrent = await callWithCallback(transport, 'add', [descriptor.magnetURI, { announce: TRACKERS }]);
        await waitForDone(torrent, timeoutMs);
        const identity = torrentIdentity(torrent);
        if (!Array.isArray(torrent.files) || torrent.files.length !== 1) fail('package-file-count');
        if (torrent.files[0].length !== descriptor.sizeBytes) fail('package-size-mismatch');
        const bytes = await fileBytes(torrent.files[0]);
        if (bytes.byteLength !== descriptor.sizeBytes) fail('package-size-mismatch');
        if (await sha256Hex(bytes) !== descriptor.sha256) fail('package-digest-mismatch');
        return Object.freeze({
          schemaVersion: 'vibapp.roomhash-package-transfer.experimental-v1',
          operation: 'fetch',
          sha256: descriptor.sha256,
          sizeBytes: descriptor.sizeBytes,
          bytes,
          ...identity,
        });
      } catch (error) {
        if (torrent?.infoHash && typeof transport.remove === 'function') {
          try { await callWithCallback(transport, 'remove', [torrent.infoHash]); } catch {}
        }
        throw error;
      } finally {
        releaseTransfer(descriptor.sizeBytes);
      }
    })();
    fetches.set(key, operation);
    try {
      const result = await operation;
      return result;
    } catch (error) {
      lastError = error instanceof Error ? error.message.slice(0, 256) : 'fetch-failed';
      throw error;
    } finally {
      if (fetches.get(key) === operation) fetches.delete(key);
    }
  }

  async function fetchVerifiedCandidate(input) {
    const locator = candidateLocator(input);
    const timeoutMs = boundedInteger(input.timeoutMs, 30_000, 1_000, MAX_TIMEOUT_MS, 'candidate-fetch-timeout');
    const key = JSON.stringify([
      locator.packageDigestSha256,
      locator.magnetURI,
      locator.files.map(file => [file.path, file.sha256, file.sizeBytes]),
    ]);
    const existing = candidateFetches.get(key);
    if (existing) return cloneCandidateTransfer(await existing);
    const transport = reserveTransfer(locator.totalBytes);
    const operation = (async () => {
      let torrent;
      try {
        torrent = await callWithCallback(transport, 'add', [locator.magnetURI, { announce: locator.trackers }]);
        await waitForDone(torrent, timeoutMs);
        const identity = torrentIdentity(torrent, locator.infoHash);
        const files = await downloadedCandidateFiles(torrent, locator.files);
        await verifyCandidateInventory(files, locator.packageDigestSha256);
        return Object.freeze({
          schemaVersion: 'vibapp.roomhash-candidate-transfer.experimental-v1',
          operation: 'fetch-candidate',
          packageDigestSha256: locator.packageDigestSha256,
          sizeBytes: locator.totalBytes,
          files,
          ...identity,
        });
      } catch (error) {
        if (torrent?.infoHash && typeof transport.remove === 'function') {
          try { await callWithCallback(transport, 'remove', [torrent.infoHash]); } catch {}
        }
        throw error;
      } finally {
        releaseTransfer(locator.totalBytes);
      }
    })();
    candidateFetches.set(key, operation);
    try {
      return cloneCandidateTransfer(await operation);
    } catch (error) {
      lastError = error instanceof Error ? error.message.slice(0, 256) : 'candidate-fetch-failed';
      throw error;
    } finally {
      if (candidateFetches.get(key) === operation) candidateFetches.delete(key);
    }
  }

  async function fetchVerifiedCandidateToStore(input) {
    if (!candidateStore) fail('candidate-store-unavailable');
    const locator = candidateLocator(input);
    const timeoutMs = boundedInteger(input.timeoutMs, 30_000, 1_000, MAX_TIMEOUT_MS, 'candidate-fetch-timeout');
    const key = JSON.stringify([
      locator.packageDigestSha256,
      locator.infoHash,
      locator.magnetURI,
      locator.files.map(file => [file.path, file.sha256, file.sizeBytes]),
    ]);
    const existing = candidateStoreFetches.get(key);
    if (existing) return existing;
    const operation = (async () => {
      let stage;
      let torrent;
      let transport;
      let reserved = false;
      try {
        stage = await candidateStore.begin({
          packageDigestSha256: locator.packageDigestSha256,
          sizeBytes: locator.totalBytes,
          files: locator.files,
        });
        if (stage?.alreadyCommitted === true) return candidateStoreReceipt(await stage.commit(), locator);
        if (
          !stage
          || typeof stage.writeFile !== 'function'
          || typeof stage.commit !== 'function'
          || typeof stage.abort !== 'function'
        ) fail('candidate-store-stage');
        transport = reserveTransfer(locator.totalBytes);
        reserved = true;
        torrent = await callWithCallback(transport, 'add', [locator.magnetURI, { announce: locator.trackers }]);
        await waitForDone(torrent, timeoutMs);
        torrentIdentity(torrent, locator.infoHash);
        await stageCandidateFiles(torrent, locator.files, locator.packageDigestSha256, stage);
        return candidateStoreReceipt(await stage.commit(), locator);
      } catch (error) {
        try { await stage?.abort?.(); } catch {}
        if (torrent?.infoHash && transport && typeof transport.remove === 'function') {
          try { await callWithCallback(transport, 'remove', [torrent.infoHash]); } catch {}
        }
        throw error;
      } finally {
        if (reserved) releaseTransfer(locator.totalBytes);
      }
    })();
    candidateStoreFetches.set(key, operation);
    try {
      return await operation;
    } catch (error) {
      lastError = error instanceof Error ? error.message.slice(0, 256) : 'candidate-store-fetch-failed';
      throw error;
    } finally {
      if (candidateStoreFetches.get(key) === operation) candidateStoreFetches.delete(key);
    }
  }

  async function stop() {
    if (state === 'stopped') return status();
    if (state === 'stopping') return stopPromise;
    if (state === 'starting') {
      try { await startPromise; } catch { return status(); }
      if (state === 'stopping') return stopPromise;
      if (state === 'stopped') return status();
    }
    state = 'stopping';
    const doomed = client;
    stopPromise = (async () => {
      try {
        if (doomed) await callWithCallback(doomed, 'destroy', []);
      } finally {
        client = null;
        settings = null;
        activeTransfers = 0;
        inFlightBytes = 0;
        seeds.clear();
        fetches.clear();
        candidateFetches.clear();
        candidateStoreFetches.clear();
        state = 'stopped';
        stopPromise = null;
      }
      return status();
    })();
    return stopPromise;
  }

  return Object.freeze({
    start,
    seedVerifiedPackage,
    fetchVerifiedPackage,
    fetchVerifiedCandidate,
    fetchVerifiedCandidateToStore,
    status,
    stop,
  });
}

const singleton = createRoomHashBrowserNode();
export const start = settings => singleton.start(settings);
export const seedVerifiedPackage = input => singleton.seedVerifiedPackage(input);
export const fetchVerifiedPackage = input => singleton.fetchVerifiedPackage(input);
export const fetchVerifiedCandidate = input => singleton.fetchVerifiedCandidate(input);
export const status = () => singleton.status();
export const stop = () => singleton.stop();
