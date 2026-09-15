import { createHash } from 'node:crypto';
import { File } from 'node:buffer';
import { lstat, mkdir, readFile, realpath, readdir, rm } from 'node:fs/promises';
import { delimiter, dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const REQUEST_SCHEMA = 'vibapp.roomhash-host-request.experimental-v1';
const RESPONSE_SCHEMA = 'vibapp.roomhash-host-response.experimental-v1';
const MAX_LINE_BYTES = 512 * 1024;
const MAX_JSON_BYTES = 1024 * 1024;
const MAX_PACKAGE_BYTES = 64 * 1024 * 1024;
const MAX_PACKAGE_FILES = 128;
const MAX_PACKAGE_TREE_ENTRIES = 4096;
const MAX_PACKAGE_TREE_DEPTH = 32;
const MAX_TORRENTS = 64;
const MAX_TIMEOUT_MS = 120_000;
const MAX_COLLABORATION_SESSIONS = 16;
const MAX_COLLABORATION_MESSAGE_BYTES = 64 * 1024;
const MAX_COLLABORATION_TTL_MS = 24 * 60 * 60 * 1000;
const MAX_COLLABORATION_INBOX_MESSAGES = 128;
const MAX_COLLABORATION_INBOX_BYTES = 1024 * 1024;
const MAX_COLLABORATION_RECEIVE_BATCH = 4;
const MAX_CONSUMED_GRANTS = 1024;
const COLLABORATION_GRANT_SCHEMA = 'vibapp.collaboration-grant.experimental-v1';
const SHA256 = /^[0-9a-f]{64}$/;
const INFO_HASH = /^[0-9a-f]{40}$/i;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const NIL_UUID = '00000000-0000-0000-0000-000000000000';

class HostError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'HostError';
    this.code = code;
  }
}

function fail(code, message) {
  throw new HostError(code, message);
}

function record(value, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    fail('invalid-request', `${label} must be an object`);
  }
  return value;
}

function exact(value, keys, label) {
  const object = record(value, label);
  const actual = Object.keys(object).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    fail('invalid-request', `${label} contains missing or unknown keys`);
  }
  return object;
}

function integer(value, minimum, maximum, label) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    fail('invalid-request', `${label} is outside its supported range`);
  }
  return value;
}

function boundedString(value, maximum, label) {
  if (typeof value !== 'string' || value.length === 0 || value.length > maximum || /[\u0000-\u001f\u007f]/.test(value)) {
    fail('invalid-request', `${label} is invalid`);
  }
  return value;
}

function digestString(value, label) {
  if (typeof value !== 'string' || !SHA256.test(value)) fail('invalid-request', `${label} is invalid`);
  return value;
}

function uuidString(value, label) {
  if (typeof value !== 'string' || !UUID.test(value) || value.toLowerCase() === NIL_UUID) {
    fail('invalid-request', `${label} must be a non-nil RFC 4122 UUID`);
  }
  return value.toLowerCase();
}

function strictBase64(value) {
  if (typeof value !== 'string' || value.length < 4 || value.length > 87_384 || value.length % 4 !== 0
    || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value)) {
    fail('invalid-request', 'message_base64 is not canonical base64');
  }
  const bytes = Buffer.from(value, 'base64');
  if (bytes.length < 1 || bytes.length > MAX_COLLABORATION_MESSAGE_BYTES || bytes.toString('base64') !== value) {
    fail('resource-limit', `collaboration message must contain 1..${MAX_COLLABORATION_MESSAGE_BYTES} bytes`);
  }
  return bytes;
}

function safeRelativePath(value, label) {
  boundedString(value, 512, label);
  if (value.startsWith('/') || value.startsWith('\\') || value.includes('\\') || value.split('/').some(part => !part || part === '.' || part === '..')) {
    fail('invalid-request', `${label} is not a safe relative path`);
  }
  return value;
}

function sha256(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

function publicError(error) {
  const code = typeof error?.code === 'string' && /^[a-z0-9-]{1,64}$/.test(error.code)
    ? error.code
    : 'internal-error';
  let message = error instanceof Error ? error.message : 'RoomHash host operation failed';
  message = String(message).replace(/[\u0000-\u001f\u007f]/g, ' ').slice(0, 256);
  return { code, message };
}

function response(requestId, ok, result = null, error = null) {
  return {
    schema: RESPONSE_SCHEMA,
    request_id: requestId,
    ok,
    result: ok ? result : null,
    error: ok ? null : error,
  };
}

function writeResponse(value) {
  const encoded = JSON.stringify(value);
  if (Buffer.byteLength(encoded) + 1 > MAX_LINE_BYTES) {
    return Promise.reject(new Error('response exceeds JSONL limit'));
  }
  return new Promise((resolveWrite, rejectWrite) => {
    process.stdout.write(`${encoded}\n`, error => error ? rejectWrite(error) : resolveWrite());
  });
}

async function requiredDirectory(name, { create = false } = {}) {
  const configured = process.env[name];
  if (!configured || !isAbsolute(configured)) fail('invalid-environment', `${name} must be an absolute directory`);
  if (create) {
    await mkdir(configured, { recursive: true, mode: 0o700 });
  }
  const metadata = await lstat(configured).catch(() => null);
  if (!metadata?.isDirectory() || metadata.isSymbolicLink()) fail('invalid-environment', `${name} is not a safe directory`);
  return realpath(configured);
}

const roomHashRoot = await requiredDirectory('VIBAPP_ROOMHASH_ROOT');
const dataDir = await requiredDirectory('VIBAPP_ROOMHASH_DATA_DIR', { create: true });
const allowedRootValues = String(process.env.VIBAPP_ROOMHASH_ALLOWED_SEED_ROOTS || '')
  .split(delimiter)
  .filter(Boolean);
if (allowedRootValues.length === 0 || allowedRootValues.length > 8) {
  fail('invalid-environment', 'VIBAPP_ROOMHASH_ALLOWED_SEED_ROOTS is invalid');
}
const allowedSeedRoots = [];
for (const path of allowedRootValues) {
  if (!isAbsolute(path)) fail('invalid-environment', 'seed roots must be absolute');
  const metadata = await lstat(path).catch(() => null);
  if (!metadata?.isDirectory() || metadata.isSymbolicLink()) fail('invalid-environment', 'seed root is not a safe directory');
  allowedSeedRoots.push(await realpath(path));
}
function isWithin(path, root) {
  const suffix = relative(root, path);
  return suffix === '' || (!suffix.startsWith(`..${sep}`) && suffix !== '..' && !isAbsolute(suffix));
}

async function privateChildDirectory(parent, name, label) {
  const path = join(parent, name);
  let metadata = await lstat(path).catch(error => {
    if (error?.code === 'ENOENT') return null;
    throw error;
  });
  if (!metadata) {
    try {
      await mkdir(path, { mode: 0o700 });
    } catch (error) {
      if (error?.code !== 'EEXIST') throw error;
    }
    metadata = await lstat(path).catch(() => null);
  }
  if (!metadata?.isDirectory() || metadata.isSymbolicLink()) {
    fail('invalid-environment', `${label} is not a safe directory`);
  }
  const canonical = await realpath(path);
  if (dirname(canonical) !== parent) fail('invalid-environment', `${label} escaped managed storage`);
  const finalMetadata = await lstat(path).catch(() => null);
  if (!finalMetadata?.isDirectory() || finalMetadata.isSymbolicLink()
    || finalMetadata.dev !== metadata.dev || finalMetadata.ino !== metadata.ino) {
    fail('invalid-environment', `${label} changed while being resolved`);
  }
  return canonical;
}

const downloadsRoot = await privateChildDirectory(dataDir, 'downloads', 'download root');

async function allowedExistingPath(path, kind) {
  boundedString(path, 4096, 'path');
  if (!isAbsolute(path)) fail('path-denied', 'path must be absolute');
  const metadata = await lstat(path).catch(() => null);
  if (!metadata || metadata.isSymbolicLink()) fail('path-denied', 'path is missing or linked');
  if (kind === 'file' && !metadata.isFile()) fail('path-denied', 'path is not a regular file');
  if (kind === 'directory' && !metadata.isDirectory()) fail('path-denied', 'path is not a directory');
  const canonical = await realpath(path);
  if (!allowedSeedRoots.some(root => isWithin(canonical, root))) fail('path-denied', 'path is outside trusted seed roots');
  return { path: canonical, metadata };
}

async function readJson(path, maximum = MAX_JSON_BYTES) {
  const metadata = await lstat(path).catch(() => null);
  if (!metadata?.isFile() || metadata.isSymbolicLink() || metadata.size < 2 || metadata.size > maximum) {
    fail('integrity-failure', 'JSON input is not a bounded regular file');
  }
  const bytes = await readFile(path);
  try {
    return { bytes, value: JSON.parse(bytes.toString('utf8')) };
  } catch {
    fail('integrity-failure', 'JSON input is malformed');
  }
}

async function hashFile(path, expectedSize, expectedDigest) {
  const metadata = await lstat(path).catch(() => null);
  if (!metadata?.isFile() || metadata.isSymbolicLink() || metadata.size !== expectedSize || metadata.size > MAX_PACKAGE_BYTES) {
    fail('integrity-failure', 'package file size or type differs from its descriptor');
  }
  const bytes = await readFile(path);
  if (sha256(bytes) !== expectedDigest) fail('integrity-failure', 'package file digest differs from its descriptor');
}

function descriptor(value, label) {
  const item = record(value, label);
  const path = safeRelativePath(item.path, `${label}.path`);
  const digest = digestString(item.sha256, `${label}.sha256`);
  const size = integer(item.size_bytes, 0, MAX_PACKAGE_BYTES, `${label}.size_bytes`);
  return { path, sha256: digest, size_bytes: size };
}

function manifestDescriptors(manifest) {
  const artifacts = record(record(manifest, 'manifest').artifacts, 'manifest.artifacts');
  const values = [
    artifacts.canonical_component,
    ...(Array.isArray(artifacts.assets) ? artifacts.assets : fail('integrity-failure', 'manifest assets are malformed')),
    artifacts.provenance,
    artifacts.sbom,
  ];
  if (!Array.isArray(artifacts.browser_derivations)) fail('integrity-failure', 'manifest browser derivations are malformed');
  for (const derivation of artifacts.browser_derivations) {
    if (!Array.isArray(derivation?.files)) fail('integrity-failure', 'browser derivation files are malformed');
    values.push(...derivation.files, derivation.derivation_attestation);
  }
  return values.map((value, index) => descriptor(value, `manifest descriptor ${index}`));
}

async function walkRegularFiles(root) {
  const output = [];
  const pending = [{ prefix: '', depth: 0 }];
  let observedEntries = 0;
  while (pending.length > 0) {
    const { prefix, depth } = pending.pop();
    const entries = await readdir(join(root, prefix), { withFileTypes: true });
    observedEntries += entries.length;
    if (observedEntries > MAX_PACKAGE_TREE_ENTRIES) {
      fail('resource-limit', 'candidate tree contains too many entries');
    }
    for (const entry of entries.sort((left, right) => Buffer.from(right.name).compare(Buffer.from(left.name)))) {
      if (entry.isSymbolicLink()) fail('integrity-failure', 'candidate tree contains a symbolic link');
      const child = prefix ? `${prefix}/${entry.name}` : entry.name;
      safeRelativePath(child, 'candidate file path');
      if (entry.isDirectory()) {
        if (depth + 1 > MAX_PACKAGE_TREE_DEPTH) fail('resource-limit', 'candidate tree is too deep');
        pending.push({ prefix: child, depth: depth + 1 });
      } else if (entry.isFile()) {
        output.push(child);
        if (output.length > MAX_PACKAGE_FILES) fail('resource-limit', 'candidate contains too many files');
      } else {
        fail('integrity-failure', 'candidate tree contains a non-regular entry');
      }
    }
  }
  output.sort((left, right) => Buffer.from(left).compare(Buffer.from(right)));
  return output;
}

async function verifiedCandidate(input) {
  const args = exact(input, ['candidate_path', 'package_digest_sha256'], 'seed-package args');
  const expectedDigest = digestString(args.package_digest_sha256, 'package_digest_sha256');
  const candidatePath = (await allowedExistingPath(args.candidate_path, 'file')).path;
  if (candidatePath !== join(dirname(candidatePath), 'candidate.json') || dirname(candidatePath).split(sep).at(-1) !== expectedDigest) {
    fail('integrity-failure', 'candidate path is not digest-bound');
  }
  const candidateRoot = dirname(candidatePath);
  const candidateDocument = await readJson(candidatePath);
  const candidate = record(candidateDocument.value, 'candidate');
  if (candidate.schema_version !== 'vibapp.builder-candidate.experimental-v1'
    || candidate.document_type !== 'verifier-promoted-candidate'
    || candidate.state !== 'candidate-ready'
    || candidate.package_directory !== 'package'
    || candidate.package_digest_sha256 !== expectedDigest
    || candidate.authority?.install !== 'daemon'
    || candidate.authority?.publish !== 'none'
    || candidate.verification?.authority !== 'independent-verifier'
    || !Array.isArray(candidate.verification?.checks)
    || candidate.verification.checks.length === 0
    || candidate.verification.checks.some(check => check?.outcome !== 'pass')) {
    fail('integrity-failure', 'candidate verification authority is invalid');
  }
  const packageDir = join(candidateRoot, 'package');
  const packageCanonical = (await allowedExistingPath(packageDir, 'directory')).path;
  if (dirname(packageCanonical) !== candidateRoot) fail('integrity-failure', 'candidate package directory escaped its root');
  const manifestItem = descriptor(candidate.manifest, 'candidate.manifest');
  const componentItem = descriptor(candidate.component, 'candidate.component');
  if (manifestItem.path !== 'manifest.json' || componentItem.path !== 'component.wasm') {
    fail('integrity-failure', 'candidate canonical paths are invalid');
  }
  await hashFile(join(packageCanonical, manifestItem.path), manifestItem.size_bytes, manifestItem.sha256);
  const manifest = (await readJson(join(packageCanonical, manifestItem.path))).value;
  const descriptors = manifestDescriptors(manifest);
  const canonicalComponent = descriptors.find(item => item.path === 'component.wasm');
  if (!canonicalComponent || canonicalComponent.sha256 !== componentItem.sha256 || canonicalComponent.size_bytes !== componentItem.size_bytes) {
    fail('integrity-failure', 'candidate component does not match manifest');
  }
  const seen = new Set(['candidate.json', 'package/manifest.json']);
  const inventory = [
    { path: 'candidate.json', sha256: sha256(candidateDocument.bytes), size_bytes: candidateDocument.bytes.length },
    { path: 'package/manifest.json', sha256: manifestItem.sha256, size_bytes: manifestItem.size_bytes },
  ];
  let totalBytes = inventory.reduce((sum, item) => sum + item.size_bytes, 0);
  for (const item of descriptors) {
    const candidateRelative = `package/${item.path}`;
    if (seen.has(candidateRelative)) fail('integrity-failure', 'candidate descriptors contain duplicate paths');
    seen.add(candidateRelative);
    await hashFile(join(packageCanonical, item.path), item.size_bytes, item.sha256);
    inventory.push({ path: candidateRelative, sha256: item.sha256, size_bytes: item.size_bytes });
    totalBytes += item.size_bytes;
  }
  if (totalBytes > MAX_PACKAGE_BYTES) fail('resource-limit', 'candidate exceeds the package byte limit');
  const observed = await walkRegularFiles(candidateRoot);
  if (observed.length !== seen.size || observed.some(path => !seen.has(path))) {
    fail('integrity-failure', 'candidate tree contains missing or unverified files');
  }
  const seedFiles = [];
  for (const item of inventory) {
    seedFiles.push(new File([await readFile(join(candidateRoot, item.path))], item.path));
  }
  return {
    candidateRoot,
    expectedDigest,
    inventory,
    seedFiles,
    totalBytes,
  };
}

function normalizeConfig(value) {
  const config = exact(value, [
    'upload_limit_bps', 'download_limit_bps', 'max_conns', 'dht', 'lsd', 'pex', 'tracker_urls', 'turn',
  ], 'configure args');
  const normalized = {
    upload_limit_bps: integer(config.upload_limit_bps, 0, 1024 ** 3, 'upload_limit_bps'),
    download_limit_bps: integer(config.download_limit_bps, 0, 1024 ** 3, 'download_limit_bps'),
    max_conns: integer(config.max_conns, 1, 128, 'max_conns'),
    dht: config.dht,
    lsd: config.lsd,
    pex: config.pex,
    tracker_urls: config.tracker_urls,
    turn: config.turn,
  };
  for (const key of ['dht', 'lsd', 'pex']) {
    if (typeof normalized[key] !== 'boolean') fail('invalid-request', `${key} must be boolean`);
  }
  if (!Array.isArray(normalized.tracker_urls) || normalized.tracker_urls.length > 16) {
    fail('invalid-request', 'tracker_urls is invalid');
  }
  normalized.tracker_urls = normalized.tracker_urls.map(url => {
    boundedString(url, 2048, 'tracker URL');
    if (!url.startsWith('wss://') || /[\s@#]/.test(url)) fail('invalid-request', 'tracker URL is invalid');
    return url;
  });
  if (new Set(normalized.tracker_urls).size !== normalized.tracker_urls.length) fail('invalid-request', 'tracker URLs contain duplicates');
  if (normalized.turn !== null) {
    const turn = exact(normalized.turn, ['urls', 'username', 'credential'], 'TURN configuration');
    if (!Array.isArray(turn.urls) || turn.urls.length === 0 || turn.urls.length > 8) fail('invalid-request', 'TURN urls are invalid');
    turn.urls.forEach(url => {
      boundedString(url, 2048, 'TURN URL');
      if (!(url.startsWith('turn:') || url.startsWith('turns:')) || /[\s@#]/.test(url)) fail('invalid-request', 'TURN URL is invalid');
    });
    boundedString(turn.username, 256, 'TURN username');
    boundedString(turn.credential, 512, 'TURN credential');
    normalized.turn = { urls: [...turn.urls], username: turn.username, credential: turn.credential };
  }
  return normalized;
}

function waitForTorrent(torrent, event, timeoutMs) {
  if ((event === 'done' && torrent?.done) || (event === 'ready' && torrent?.ready)) return Promise.resolve(torrent);
  return new Promise((resolveWait, rejectWait) => {
    const timeout = setTimeout(() => finish(new HostError('transfer-timeout', `torrent ${event} timed out`)), timeoutMs);
    const finish = error => {
      clearTimeout(timeout);
      torrent.removeListener?.(event, onSuccess);
      torrent.removeListener?.('error', onError);
      error ? rejectWait(error) : resolveWait(torrent);
    };
    const onSuccess = () => finish();
    const onError = error => finish(error || new HostError('transfer-failed', 'torrent failed'));
    torrent.once(event, onSuccess);
    torrent.once('error', onError);
  });
}

function seedPath(client, input, name, announce) {
  return new Promise((resolveSeed, rejectSeed) => {
    let settled = false;
    const timeout = setTimeout(() => finish(new HostError('transfer-timeout', 'torrent seed timed out')), 30_000);
    const finish = (error, torrent) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      error ? rejectSeed(error) : resolveSeed(torrent);
    };
    try {
      client.seed(input, { name, announce }, torrent => finish(null, torrent));
    } catch (error) {
      finish(error);
    }
  });
}

let configured = null;
let torrentService = null;
let collaborationAdapter = null;
let collaborationCapability = {
  available: false,
  state: 'stopped',
  implementation: 'roomhash-current-trystero-torrent-0.25.3-werift',
  reason_code: null,
  turn_configured: false,
  turn_applied: false,
};
let activeTransfers = 0;
let hostErrors = 0;
const packageSeeds = new Map();
const consumedCollaborationGrants = new Map();
const collaborationInbox = [];
let collaborationInboxBytes = 0;

function roomHashConfig(config) {
  return {
    dataDir,
    ice: {
      mesh: { portMin: 44000, portMax: 44031, maxActive: Math.min(MAX_COLLABORATION_SESSIONS, config.max_conns) },
      torrent: { portMin: 44151, portMax: 44200, maxActive: Math.min(24, config.max_conns) },
      connectionTimeoutMs: 45_000,
      bindAddresses: [],
      additionalHostAddresses: [],
    },
    torrent: {
      enabled: true,
      autoCache: false,
      uploadLimit: config.upload_limit_bps || -1,
      downloadLimit: config.download_limit_bps || -1,
      maxConns: config.max_conns,
      trackerOffers: 0,
      port: 0,
      dhtPort: 0,
      dht: config.dht,
      lsd: config.lsd,
      pex: config.pex,
      utp: false,
      webRtcTrackers: config.tracker_urls,
      upnp: false,
      natPmp: false,
    },
  };
}

function collaborationStatus() {
  const adapterStatus = collaborationAdapter?.status?.() || null;
  return {
    ...collaborationCapability,
    session_count: adapterStatus?.session_count || 0,
    limits: {
      max_sessions: Math.min(MAX_COLLABORATION_SESSIONS, configured?.max_conns || MAX_COLLABORATION_SESSIONS),
      max_message_bytes: MAX_COLLABORATION_MESSAGE_BYTES,
      max_session_ttl_ms: MAX_COLLABORATION_TTL_MS,
    },
    sessions: adapterStatus?.sessions || [],
    queued_messages: collaborationInbox.length,
    queued_bytes: collaborationInboxBytes,
  };
}

function discardCollaborationInbox(sessionId = null) {
  for (let index = collaborationInbox.length - 1; index >= 0; index -= 1) {
    if (sessionId !== null && collaborationInbox[index].session_id !== sessionId) continue;
    collaborationInboxBytes -= collaborationInbox[index].size_bytes;
    collaborationInbox.splice(index, 1);
  }
  collaborationInboxBytes = Math.max(0, collaborationInboxBytes);
}

function status() {
  const source = torrentService?.status?.() || {};
  return {
    status: torrentService?.client ? 'running' : 'stopped',
    started: Boolean(torrentService?.client),
    implementation: 'roomhash-current-headless-webtorrent-3.0.16',
    torrent_port: source.gateway?.tcpPort || null,
    torrent_count: source.torrentCount || 0,
    active_transfers: activeTransfers,
    error_count: hostErrors + (source.errors || 0),
    collaboration: collaborationStatus(),
  };
}

function cleanupConsumedGrants(now = Date.now()) {
  for (const [grantId, expiresAt] of consumedCollaborationGrants) {
    if (expiresAt <= now) consumedCollaborationGrants.delete(grantId);
  }
}

async function authorizeCollaborationGrant(value, request) {
  const grant = exact(value, [
    'schema', 'grant_id', 'approved', 'operation', 'channel_id', 'expires_at',
  ], 'collaboration grant');
  if (grant.schema !== COLLABORATION_GRANT_SCHEMA || grant.approved !== true) {
    fail('grant-denied', 'collaboration grant is not an explicit approval');
  }
  const grantId = uuidString(grant.grant_id, 'collaboration grant_id');
  const channelId = uuidString(grant.channel_id, 'collaboration grant channel_id');
  if (!['create', 'join'].includes(grant.operation)
    || grant.operation !== request.operation
    || channelId !== request.channel_id
    || grant.expires_at !== request.expires_at) {
    fail('grant-denied', 'collaboration grant does not match the requested operation');
  }
  const now = Date.now();
  integer(grant.expires_at, now + 1, now + MAX_COLLABORATION_TTL_MS, 'collaboration grant expires_at');
  cleanupConsumedGrants(now);
  if (consumedCollaborationGrants.has(grantId)) fail('grant-replayed', 'collaboration grant was already consumed');
  if (consumedCollaborationGrants.size >= MAX_CONSUMED_GRANTS) fail('resource-limit', 'collaboration grant history limit reached');
  consumedCollaborationGrants.set(grantId, grant.expires_at);
  return true;
}

async function startCollaboration() {
  collaborationAdapter = null;
  collaborationCapability = {
    available: false,
    state: 'unavailable',
    implementation: 'roomhash-current-trystero-torrent-0.25.3-werift',
    reason_code: null,
    turn_configured: configured.turn !== null,
    turn_applied: false,
  };
  // RoomHash's current rtc-config does not apply custom TURN credentials to
  // its managed Werift connection. Do not silently discard that setting and
  // never let it prevent the independent WebTorrent service from starting.
  if (configured.turn !== null) {
    collaborationCapability.reason_code = 'turn-unsupported';
    return;
  }
  try {
    const hostDirectory = dirname(fileURLToPath(import.meta.url));
    const sourceRoot = process.env.VIBAPP_ROOMHASH_COLLABORATION_ROOT
      || resolve(hostDirectory, '../roomhash-collaboration/src');
    if (!isAbsolute(sourceRoot)) fail('invalid-environment', 'VIBAPP_ROOMHASH_COLLABORATION_ROOT must be absolute');
    const metadata = await lstat(sourceRoot).catch(() => null);
    if (!metadata?.isDirectory() || metadata.isSymbolicLink()) {
      fail('implementation-unavailable', 'RoomHash collaboration adapter is unavailable');
    }
    const [adapterModule, factoryModule] = await Promise.all([
      import(pathToFileURL(join(sourceRoot, 'index.mjs')).href),
      import(pathToFileURL(join(sourceRoot, 'roomhash-factory.mjs')).href),
    ]);
    if (typeof adapterModule.CollaborationAdapter !== 'function'
      || typeof factoryModule.createRoomHashTransport !== 'function') {
      fail('implementation-unavailable', 'RoomHash collaboration exports are invalid');
    }
    const transport = await factoryModule.createRoomHashTransport({
      roomhash_root: roomHashRoot,
      config: {
        ...roomHashConfig(configured),
        tracker: configured.tracker_urls[0] || 'wss://tracker.openwebtorrent.com',
      },
    });
    collaborationAdapter = new adapterModule.CollaborationAdapter({
      transport,
      authorize: authorizeCollaborationGrant,
      onMessage(input) {
        if (collaborationInbox.length >= MAX_COLLABORATION_INBOX_MESSAGES
          || collaborationInboxBytes + input.message.byteLength > MAX_COLLABORATION_INBOX_BYTES) {
          return false;
        }
        collaborationInbox.push({
          session_id: input.session_id,
          channel_id: input.channel_id,
          received_at: Date.now(),
          size_bytes: input.message.byteLength,
          message_base64: Buffer.from(input.message).toString('base64'),
        });
        collaborationInboxBytes += input.message.byteLength;
        return true;
      },
    });
    collaborationCapability = {
      ...collaborationCapability,
      available: true,
      state: 'ready',
    };
  } catch (error) {
    hostErrors += 1;
    collaborationAdapter = null;
    collaborationCapability.reason_code = publicError(error).code;
  }
}

async function startHost() {
  if (torrentService?.client) return status();
  if (!configured) fail('not-configured', 'RoomHash host must be configured before start');
  const modulePath = join(roomHashRoot, 'headless', 'src', 'torrent-service.js');
  const metadata = await lstat(modulePath).catch(() => null);
  if (!metadata?.isFile() || metadata.isSymbolicLink()) fail('implementation-unavailable', 'RoomHash TorrentService is unavailable');
  const { TorrentService } = await import(pathToFileURL(modulePath).href);
  if (typeof TorrentService !== 'function') fail('implementation-unavailable', 'RoomHash TorrentService export is invalid');
  const next = new TorrentService({
    config: roomHashConfig(configured),
    stateStore: { async addMagnet() {} },
    log(event) {
      if (String(event).endsWith('_error')) hostErrors += 1;
    },
  });
  await next.start([]);
  torrentService = next;
  await startCollaboration();
  return status();
}

async function seedPackage(args) {
  if (!torrentService?.client) fail('not-running', 'RoomHash host is not running');
  if (torrentService.client.torrents.length >= MAX_TORRENTS) fail('resource-limit', 'torrent limit reached');
  const verified = await verifiedCandidate(args);
  if (packageSeeds.has(verified.expectedDigest)) return packageSeeds.get(verified.expectedDigest);
  activeTransfers += 1;
  try {
    const torrent = await seedPath(
      torrentService.client,
      verified.seedFiles,
      `${verified.expectedDigest}.vibapp-candidate`,
      configured.tracker_urls,
    );
    if (!INFO_HASH.test(torrent?.infoHash || '') || typeof torrent?.magnetURI !== 'string') {
      fail('transfer-failed', 'RoomHash returned an invalid torrent identity');
    }
    const result = {
      operation: 'seed-package',
      package_digest_sha256: verified.expectedDigest,
      info_hash: torrent.infoHash.toLowerCase(),
      magnet_uri: torrent.magnetURI,
      size_bytes: verified.totalBytes,
      files: verified.inventory,
    };
    packageSeeds.set(verified.expectedDigest, result);
    return result;
  } finally {
    activeTransfers -= 1;
  }
}

async function seedFile(value) {
  if (!torrentService?.client) fail('not-running', 'RoomHash host is not running');
  const args = exact(value, ['path', 'sha256', 'size_bytes', 'name'], 'seed args');
  const expectedDigest = digestString(args.sha256, 'sha256');
  const expectedSize = integer(args.size_bytes, 1, MAX_PACKAGE_BYTES, 'size_bytes');
  const name = boundedString(args.name, 128, 'name');
  if (!/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$/.test(name)) fail('invalid-request', 'name is invalid');
  const source = await allowedExistingPath(args.path, 'file');
  await hashFile(source.path, expectedSize, expectedDigest);
  activeTransfers += 1;
  try {
    const torrent = await seedPath(torrentService.client, source.path, name, configured.tracker_urls);
    return {
      operation: 'seed',
      sha256: expectedDigest,
      size_bytes: expectedSize,
      info_hash: torrent.infoHash.toLowerCase(),
      magnet_uri: torrent.magnetURI,
    };
  } finally {
    activeTransfers -= 1;
  }
}

function expectedFetchFiles(value) {
  if (!Array.isArray(value) || value.length < 2 || value.length > MAX_PACKAGE_FILES) {
    fail('invalid-request', 'fetch files are invalid');
  }
  const seen = new Set();
  let total = 0;
  const files = value.map((item, index) => {
    const file = descriptor(exact(item, ['path', 'sha256', 'size_bytes'], `fetch file ${index}`), `fetch file ${index}`);
    if (file.size_bytes < 1 || seen.has(file.path)) fail('invalid-request', 'fetch files contain invalid or duplicate entries');
    seen.add(file.path);
    total += file.size_bytes;
    return file;
  });
  if (total > MAX_PACKAGE_BYTES) fail('resource-limit', 'fetch exceeds package byte limit');
  if (!seen.has('candidate.json') || !seen.has('package/manifest.json')) {
    fail('invalid-request', 'fetch files omit required VibApp package entries');
  }
  return files;
}

function fetchMagnetInfoHash(value, packageDigest) {
  boundedString(value, 4096, 'magnet_uri');
  let magnet;
  try {
    magnet = new URL(value);
  } catch {
    fail('invalid-request', 'magnet_uri is invalid');
  }
  if (magnet.protocol !== 'magnet:' || magnet.host || magnet.pathname
    || magnet.hash || magnet.username || magnet.password) {
    fail('invalid-request', 'magnet_uri is invalid');
  }
  const parameters = [...magnet.searchParams.entries()];
  if (parameters.length < 2 || parameters.length > 18
    || parameters.some(([key]) => !['xt', 'dn', 'tr'].includes(key))) {
    fail('invalid-request', 'magnet_uri contains unsupported parameters');
  }
  const exactTopics = magnet.searchParams.getAll('xt');
  const displayNames = magnet.searchParams.getAll('dn');
  const trackers = magnet.searchParams.getAll('tr');
  if (exactTopics.length !== 1 || displayNames.length !== 1
    || displayNames[0] !== `${packageDigest}.vibapp-candidate`
    || trackers.length !== new Set(trackers).size
    || trackers.some(tracker => {
      if (typeof tracker !== 'string' || tracker.length === 0 || tracker.length > 2048
        || /[\u0000-\u001f\u007f\s@#\\]/.test(tracker)) return true;
      try {
        const url = new URL(tracker);
        return url.protocol !== 'wss:' || !url.hostname || Boolean(url.username || url.password || url.hash);
      } catch {
        return true;
      }
    })) {
    fail('invalid-request', 'magnet_uri identity is invalid');
  }
  const match = /^urn:btih:([0-9a-f]{40})$/.exec(exactTopics[0]);
  if (!match) fail('invalid-request', 'magnet_uri info hash is invalid');
  return match[1];
}

function sameEntry(left, right) {
  return left?.dev === right?.dev && left?.ino === right?.ino;
}

async function checkedDownloadDestination(packageDigest) {
  const destination = join(downloadsRoot, packageDigest);
  const existing = await lstat(destination).catch(error => {
    if (error?.code === 'ENOENT') return null;
    throw error;
  });
  if (existing) {
    if (!existing.isDirectory() || existing.isSymbolicLink()) {
      fail('path-denied', 'download destination is unsafe');
    }
    const canonical = await realpath(destination);
    if (canonical !== destination || dirname(canonical) !== downloadsRoot) {
      fail('path-denied', 'download destination escaped managed storage');
    }
    const finalMetadata = await lstat(destination).catch(() => null);
    if (!finalMetadata?.isDirectory() || finalMetadata.isSymbolicLink() || !sameEntry(existing, finalMetadata)) {
      fail('path-denied', 'download destination changed while being resolved');
    }
    return { destination, metadata: finalMetadata, created: false };
  }
  let createdByHost = true;
  try {
    await mkdir(destination, { mode: 0o700 });
  } catch (error) {
    if (error?.code !== 'EEXIST') throw error;
    createdByHost = false;
  }
  const created = await lstat(destination).catch(() => null);
  if (!created?.isDirectory() || created.isSymbolicLink()) {
    fail('path-denied', 'download destination is unsafe');
  }
  const canonical = await realpath(destination);
  if (canonical !== destination || dirname(canonical) !== downloadsRoot) {
    fail('path-denied', 'download destination escaped managed storage');
  }
  return { destination, metadata: created, created: createdByHost };
}

async function verifyDownloadedTree(destination, expectedFiles) {
  const observed = await walkRegularFiles(destination);
  if (observed.length !== expectedFiles.length) {
    fail('integrity-failure', 'downloaded file count differs');
  }
  const used = new Set();
  let candidatePath = null;
  for (const expected of expectedFiles) {
    const matches = observed.filter(path => !used.has(path)
      && (path === expected.path || path.endsWith(`/${expected.path}`)));
    if (matches.length !== 1) fail('integrity-failure', 'downloaded file inventory differs');
    const matched = matches[0];
    used.add(matched);
    await hashFile(join(destination, matched), expected.size_bytes, expected.sha256);
    if (expected.path === 'candidate.json') candidatePath = join(destination, matched);
  }
  if (!candidatePath || used.size !== observed.length) {
    fail('integrity-failure', 'downloaded candidate entry is missing');
  }
  return candidatePath;
}

async function cleanupCreatedDownload(destination, identity) {
  const current = await lstat(destination).catch(() => null);
  if (!current?.isDirectory() || current.isSymbolicLink() || !sameEntry(current, identity)) return;
  const canonical = await realpath(destination).catch(() => null);
  if (canonical !== destination || dirname(canonical) !== downloadsRoot) return;
  await rm(destination, { recursive: true, force: true }).catch(() => {});
}

async function fetchPackage(value) {
  if (!torrentService?.client) fail('not-running', 'RoomHash host is not running');
  const args = exact(value, ['magnet_uri', 'package_digest_sha256', 'files', 'timeout_ms'], 'fetch args');
  const packageDigest = digestString(args.package_digest_sha256, 'package_digest_sha256');
  const expectedInfoHash = fetchMagnetInfoHash(args.magnet_uri, packageDigest);
  const timeoutMs = integer(args.timeout_ms, 1_000, MAX_TIMEOUT_MS, 'timeout_ms');
  const files = expectedFetchFiles(args.files);
  const { destination, metadata: destinationIdentity, created } = await checkedDownloadDestination(packageDigest);
  if (!created) {
    const candidatePath = await verifyDownloadedTree(destination, files).catch(() => null);
    if (!candidatePath) fail('conflict', 'download destination already exists but is not a verified candidate');
    return {
      operation: 'fetch',
      package_digest_sha256: packageDigest,
      info_hash: expectedInfoHash,
      candidate_path: candidatePath,
      size_bytes: files.reduce((sum, file) => sum + file.size_bytes, 0),
    };
  }
  activeTransfers += 1;
  let torrent;
  let completed = false;
  try {
    torrent = torrentService.client.add(args.magnet_uri, { path: destination, announce: configured.tracker_urls });
    await waitForTorrent(torrent, 'done', timeoutMs);
    if (!INFO_HASH.test(torrent.infoHash || '') || torrent.infoHash.toLowerCase() !== expectedInfoHash) {
      fail('integrity-failure', 'downloaded torrent identity differs');
    }
    const finalIdentity = await lstat(destination).catch(() => null);
    if (!finalIdentity?.isDirectory() || finalIdentity.isSymbolicLink() || !sameEntry(destinationIdentity, finalIdentity)) {
      fail('path-denied', 'download destination changed during transfer');
    }
    const candidatePath = await verifyDownloadedTree(destination, files);
    completed = true;
    return {
      operation: 'fetch',
      package_digest_sha256: packageDigest,
      info_hash: expectedInfoHash,
      candidate_path: candidatePath,
      size_bytes: files.reduce((sum, file) => sum + file.size_bytes, 0),
    };
  } catch (error) {
    if (torrent?.infoHash) {
      try { await torrentService.client.remove(torrent.infoHash, { destroyStore: false }); } catch {}
    }
    throw error;
  } finally {
    if (!completed) await cleanupCreatedDownload(destination, destinationIdentity);
    activeTransfers -= 1;
  }
}

async function removeTorrent(value) {
  if (!torrentService?.client) fail('not-running', 'RoomHash host is not running');
  const args = exact(value, ['info_hash'], 'remove args');
  if (typeof args.info_hash !== 'string' || !INFO_HASH.test(args.info_hash)) fail('invalid-request', 'info_hash is invalid');
  await torrentService.client.remove(args.info_hash, { destroyStore: false });
  for (const [digest, receipt] of packageSeeds) {
    if (receipt.info_hash === args.info_hash.toLowerCase()) packageSeeds.delete(digest);
  }
  return { status: 'removed', info_hash: args.info_hash.toLowerCase() };
}

function requireCollaboration() {
  if (!torrentService?.client) fail('not-running', 'RoomHash host is not running');
  if (!collaborationAdapter || !collaborationCapability.available) {
    const reason = collaborationCapability.reason_code || 'implementation-unavailable';
    fail('rtc-unavailable', `RoomHash collaboration is unavailable (${reason})`);
  }
  return collaborationAdapter;
}

function collaborationOpenArgs(value, operation) {
  const args = exact(value, ['channel_id', 'expires_at', 'grant'], `collaboration-${operation} args`);
  const channelId = uuidString(args.channel_id, 'channel_id');
  const now = Date.now();
  const expiresAt = integer(args.expires_at, now + 1, now + MAX_COLLABORATION_TTL_MS, 'expires_at');
  record(args.grant, 'grant');
  const effectiveLimit = Math.min(MAX_COLLABORATION_SESSIONS, configured.max_conns);
  if (collaborationAdapter?.status?.().session_count >= effectiveLimit) {
    fail('resource-limit', `collaboration session limit reached (${effectiveLimit})`);
  }
  return { channel_id: channelId, expires_at: expiresAt, grant: args.grant };
}

async function collaborationCreate(value) {
  const adapter = requireCollaboration();
  const args = collaborationOpenArgs(value, 'create');
  try {
    return await adapter.createAssigned(args);
  } catch (error) {
    if (error instanceof HostError) throw error;
    fail('rtc-failed', publicError(error).message);
  }
}

async function collaborationJoin(value) {
  const adapter = requireCollaboration();
  const args = collaborationOpenArgs(value, 'join');
  try {
    return await adapter.join(args);
  } catch (error) {
    if (error instanceof HostError) throw error;
    fail('rtc-failed', publicError(error).message);
  }
}

async function collaborationLeave(value) {
  const adapter = requireCollaboration();
  const args = exact(value, ['session_id'], 'collaboration-leave args');
  const sessionId = uuidString(args.session_id, 'session_id');
  try {
    const result = await adapter.leave({ session_id: sessionId });
    discardCollaborationInbox(sessionId);
    return result;
  } catch (error) {
    if (error instanceof HostError) throw error;
    fail('session-not-found', 'collaboration session is unknown or expired');
  }
}

function collaborationReceive(value) {
  const adapter = requireCollaboration();
  const args = exact(value, ['session_id', 'limit'], 'collaboration-receive args');
  const sessionId = uuidString(args.session_id, 'session_id');
  const limit = integer(args.limit, 1, MAX_COLLABORATION_RECEIVE_BATCH, 'limit');
  if (!adapter.status().sessions.some(session => session.session_id === sessionId)) {
    fail('session-not-found', 'collaboration session is unknown or expired');
  }
  const messages = [];
  for (let index = 0; index < collaborationInbox.length && messages.length < limit;) {
    if (collaborationInbox[index].session_id !== sessionId) {
      index += 1;
      continue;
    }
    const [message] = collaborationInbox.splice(index, 1);
    collaborationInboxBytes -= message.size_bytes;
    messages.push(message);
  }
  collaborationInboxBytes = Math.max(0, collaborationInboxBytes);
  return { session_id: sessionId, messages };
}

async function collaborationSend(value) {
  const adapter = requireCollaboration();
  const args = exact(value, ['session_id', 'message_base64'], 'collaboration-send args');
  const sessionId = uuidString(args.session_id, 'session_id');
  const message = strictBase64(args.message_base64);
  try {
    return await adapter.send({ session_id: sessionId, message });
  } catch (error) {
    if (error instanceof HostError) throw error;
    fail('session-not-found', 'collaboration session is unknown or expired');
  }
}

async function shutdownHost() {
  const adapter = collaborationAdapter;
  const service = torrentService;
  collaborationAdapter = null;
  torrentService = null;
  const closed = await Promise.allSettled([
    adapter?.close?.(),
    service?.close?.(),
  ]);
  packageSeeds.clear();
  consumedCollaborationGrants.clear();
  discardCollaborationInbox();
  activeTransfers = 0;
  collaborationCapability = {
    ...collaborationCapability,
    available: false,
    state: 'stopped',
    reason_code: null,
    turn_applied: false,
  };
  const rejected = closed.find(item => item.status === 'rejected');
  if (rejected) throw rejected.reason;
  return { status: 'stopped' };
}

async function dispatch(command, args) {
  switch (command) {
    case 'configure':
      if (torrentService?.client) fail('already-running', 'configuration requires a host restart');
      configured = normalizeConfig(args);
      return { status: 'configured', turn_configured: configured.turn !== null };
    case 'start':
      exact(args, [], 'start args');
      return startHost();
    case 'health':
    case 'status':
      exact(args, [], `${command} args`);
      return status();
    case 'seed':
      return seedFile(args);
    case 'seed-package':
      return seedPackage(args);
    case 'fetch':
      return fetchPackage(args);
    case 'remove':
      return removeTorrent(args);
    case 'collaboration-create':
      return collaborationCreate(args);
    case 'collaboration-join':
      return collaborationJoin(args);
    case 'collaboration-leave':
      return collaborationLeave(args);
    case 'collaboration-send':
      return collaborationSend(args);
    case 'collaboration-receive':
      return collaborationReceive(args);
    case 'collaboration-status':
      exact(args, [], 'collaboration-status args');
      return collaborationStatus();
    case 'shutdown':
      exact(args, [], 'shutdown args');
      return shutdownHost();
    default:
      fail('unsupported-command', 'RoomHash host command is unsupported');
  }
}

function validateRequest(value) {
  const request = exact(value, ['schema', 'request_id', 'command', 'args'], 'request');
  if (request.schema !== REQUEST_SCHEMA) fail('invalid-request', 'request schema is unsupported');
  integer(request.request_id, 1, Number.MAX_SAFE_INTEGER, 'request_id');
  boundedString(request.command, 64, 'command');
  record(request.args, 'args');
  return request;
}

let closeAfterQueue = false;
let queue = Promise.resolve();

async function handleLine(line) {
  let requestId = null;
  try {
    const request = validateRequest(JSON.parse(line.toString('utf8')));
    requestId = request.request_id;
    const result = await dispatch(request.command, request.args);
    await writeResponse(response(requestId, true, result));
    if (request.command === 'shutdown') closeAfterQueue = true;
  } catch (error) {
    hostErrors += 1;
    await writeResponse(response(requestId, false, null, publicError(error)));
  }
}

function enqueue(line) {
  queue = queue.then(() => handleLine(line)).catch(async error => {
    try { await writeResponse(response(null, false, null, publicError(error))); } catch {}
  }).finally(() => {
    if (closeAfterQueue) {
      process.stdin.pause();
      // The response write callback has completed. Exit explicitly so
      // Trystero relay-reconnect handles cannot outlive the trusted launcher
      // shutdown deadline after all transports have already been closed.
      setImmediate(() => process.exit(0));
    }
  });
}

let chunks = [];
let lineBytes = 0;
let inputFailed = false;
process.stdin.on('data', chunk => {
  if (inputFailed) return;
  let offset = 0;
  while (offset < chunk.length) {
    const newline = chunk.indexOf(0x0a, offset);
    const end = newline === -1 ? chunk.length : newline;
    const piece = chunk.subarray(offset, end);
    lineBytes += piece.length;
    if (lineBytes + 1 > MAX_LINE_BYTES) {
      inputFailed = true;
      enqueue(Buffer.from('{'));
      process.stdin.pause();
      return;
    }
    chunks.push(piece);
    if (newline === -1) return;
    let line = Buffer.concat(chunks, lineBytes);
    if (line.at(-1) === 0x0d) line = line.subarray(0, -1);
    chunks = [];
    lineBytes = 0;
    if (line.length === 0) {
      inputFailed = true;
      enqueue(Buffer.from('{'));
      process.stdin.pause();
      return;
    }
    enqueue(line);
    offset = newline + 1;
  }
});

process.stdin.on('end', () => {
  if (lineBytes !== 0) enqueue(Buffer.from('{'));
  queue.finally(() => shutdownHost().catch(() => {}));
});
process.on('SIGTERM', () => shutdownHost().finally(() => process.exit(0)));
process.on('SIGINT', () => shutdownHost().finally(() => process.exit(0)));

await writeResponse(response('ready', true, { status: 'ready' }));
process.stdin.resume();
