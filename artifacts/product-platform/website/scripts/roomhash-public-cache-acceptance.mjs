import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { access, mkdtemp, readFile, rm, stat } from 'node:fs/promises';
import { createReadStream } from 'node:fs';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { dirname, extname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const ARTIFACTS = resolve(HERE, '../../..');
const WORKSPACE = dirname(ARTIFACTS);
const WEB_CLIENT_CORE = join(ARTIFACTS, 'web-client-core');
const CANDIDATE_ROOT = join(ARTIFACTS, 'app-builder', 'demo-output', 'pipeline', 'candidates');
const PACKAGE_DIGEST = 'dfad1fed5fe0eb8c5bf83927eee64695287e22b5c8149d878b231d69d056e674';
const CANDIDATE_DIRECTORY = join(CANDIDATE_ROOT, PACKAGE_DIGEST);
const ROOMHASH_WEB_ROOT = resolve(
  process.env.VIBAPP_ROOMHASH_WEB_ROOT
    ?? join(WORKSPACE, '..', 'RoomHash', 'roomhash.github.io'),
);
const FIXED_PUBLIC_TRACKERS = Object.freeze([
  'wss://tracker.webtorrent.dev',
  'wss://tracker.openwebtorrent.com',
  'wss://tracker.btorrent.xyz',
]);
const LOCATOR_TRACKERS = Object.freeze([FIXED_PUBLIC_TRACKERS[0]]);
const CANDIDATE_PATHS = Object.freeze([
  'candidate.json',
  'package/component.wasm',
  'package/manifest.json',
  'package/provenance.json',
  'package/sbom.cdx.json',
]);
function sha256(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

function progress(message) {
  process.stderr.write(`[roomhash-public-cache] ${message}\n`);
}

function withTimeout(promise, milliseconds, label) {
  let timeout;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timeout = setTimeout(() => reject(new Error(`${label} timed out after ${milliseconds}ms`)), milliseconds);
    }),
  ]).finally(() => clearTimeout(timeout));
}

async function firstExecutable(candidates) {
  for (const candidate of candidates.filter(Boolean)) {
    try {
      await access(candidate);
      return candidate;
    } catch {
      // Continue to the next explicit platform location.
    }
  }
  throw new Error('Chrome was not found; set VIBAPP_CHROME_BIN to a Chromium-compatible executable');
}

class CdpPage {
  constructor(socket) {
    this.socket = socket;
    this.sequence = 0;
    this.pending = new Map();
    this.events = new Map();
    socket.onmessage = event => {
      const message = JSON.parse(String(event.data));
      if (message.id !== undefined) {
        const pending = this.pending.get(message.id);
        if (!pending) return;
        this.pending.delete(message.id);
        if (message.error) pending.reject(new Error(message.error.message));
        else pending.resolve(message.result);
        return;
      }
      const waiters = this.events.get(message.method);
      if (!waiters?.length) return;
      this.events.delete(message.method);
      for (const waiter of waiters) waiter(message.params);
    };
  }

  static async connect(url) {
    const socket = new WebSocket(url);
    await withTimeout(new Promise((resolveOpen, rejectOpen) => {
      socket.onopen = resolveOpen;
      socket.onerror = () => rejectOpen(new Error('Chrome DevTools WebSocket connection failed'));
    }), 10_000, 'Chrome DevTools connection');
    const page = new CdpPage(socket);
    await Promise.all([page.send('Runtime.enable'), page.send('Page.enable')]);
    return page;
  }

  send(method, params = {}) {
    const id = ++this.sequence;
    return withTimeout(new Promise((resolveResult, rejectResult) => {
      this.pending.set(id, { resolve: resolveResult, reject: rejectResult });
      this.socket.send(JSON.stringify({ id, method, params }));
    }), 120_000, `Chrome DevTools ${method}`);
  }

  waitFor(method) {
    return withTimeout(new Promise(resolveEvent => {
      const waiters = this.events.get(method) ?? [];
      waiters.push(resolveEvent);
      this.events.set(method, waiters);
    }), 30_000, `Chrome DevTools ${method}`);
  }

  async navigate(url) {
    const loaded = this.waitFor('Page.loadEventFired');
    await this.send('Page.navigate', { url });
    await loaded;
  }

  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate', {
      expression,
      awaitPromise: true,
      returnByValue: true,
      userGesture: true,
    });
    if (result.exceptionDetails) {
      const details = result.exceptionDetails.exception?.description
        ?? result.exceptionDetails.text
        ?? 'browser evaluation failed';
      throw new Error(details);
    }
    return result.result.value;
  }

  close() {
    this.socket.close();
  }
}

async function launchChrome() {
  const executable = await firstExecutable([
    process.env.VIBAPP_CHROME_BIN,
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
  ]);
  const profile = await mkdtemp(join(tmpdir(), 'vibapp-roomhash-chrome-'));
  const child = spawn(executable, [
    '--headless=new',
    '--remote-debugging-port=0',
    `--user-data-dir=${profile}`,
    '--disable-background-networking',
    '--disable-default-apps',
    '--disable-extensions',
    '--disable-sync',
    '--metrics-recording-only',
    '--no-first-run',
    'about:blank',
  ], { stdio: ['ignore', 'ignore', 'pipe'] });
  child.stderr.setEncoding('utf8');
  let stderr = '';
  const browserWebSocket = await withTimeout(new Promise((resolveSocket, rejectSocket) => {
    child.once('error', rejectSocket);
    child.once('exit', code => rejectSocket(new Error(`Chrome exited before DevTools became ready (${code}): ${stderr}`)));
    child.stderr.on('data', chunk => {
      stderr = (stderr + chunk).slice(-16_384);
      const match = stderr.match(/DevTools listening on (ws:\/\/[^\s]+)/);
      if (match) resolveSocket(match[1]);
    });
  }), 20_000, 'Chrome startup');
  const devtools = new URL(browserWebSocket);
  const httpOrigin = `http://${devtools.host}`;

  async function newPage(url) {
    const response = await fetch(`${httpOrigin}/json/new?${encodeURIComponent(url)}`, { method: 'PUT' });
    if (!response.ok) throw new Error(`Chrome target creation failed with HTTP ${response.status}`);
    const target = await response.json();
    return CdpPage.connect(target.webSocketDebuggerUrl);
  }

  async function close() {
    try {
      await fetch(`${httpOrigin}/json/close`, { method: 'PUT' }).catch(() => {});
    } finally {
      if (child.exitCode === null) child.kill('SIGTERM');
      await withTimeout(new Promise(resolveExit => {
        if (child.exitCode !== null) resolveExit();
        else child.once('exit', resolveExit);
      }), 5_000, 'Chrome shutdown').catch(() => {
        if (child.exitCode === null) child.kill('SIGKILL');
      });
      await rm(profile, { recursive: true, force: true });
    }
  }

  return { executable, newPage, close };
}

async function listen(server) {
  await new Promise((resolveListen, rejectListen) => {
    server.once('error', rejectListen);
    server.listen(0, '127.0.0.1', resolveListen);
  });
  const addressSource = typeof server.address === 'function' ? server : server.http;
  const address = addressSource?.address();
  if (!address || typeof address === 'string') throw new Error('server did not expose a TCP port');
  return address.port;
}

async function closeServer(server) {
  if (!server) return;
  await new Promise(resolveClose => server.close(() => resolveClose()));
}

async function candidateDescriptors() {
  const descriptors = [];
  for (const path of CANDIDATE_PATHS) {
    const bytes = await readFile(join(CANDIDATE_DIRECTORY, ...path.split('/')));
    descriptors.push({ path, sha256: sha256(bytes), size_bytes: bytes.byteLength });
  }
  const candidate = JSON.parse(await readFile(join(CANDIDATE_DIRECTORY, 'candidate.json'), 'utf8'));
  if (candidate.package_digest_sha256 !== PACKAGE_DIGEST) {
    throw new Error('production candidate digest no longer matches the acceptance fixture');
  }
  return descriptors;
}

function contentType(path) {
  switch (extname(path)) {
    case '.js':
    case '.mjs': return 'text/javascript; charset=utf-8';
    case '.json': return 'application/json; charset=utf-8';
    case '.wasm': return 'application/wasm';
    default: return 'application/octet-stream';
  }
}

const { validatePublicLocator } = await import(pathToFileURL(
  join(WEB_CLIENT_CORE, 'sync-public-package-locators.mjs'),
).href);
const { default: TrackerServer } = await import(pathToFileURL(
  join(ROOMHASH_WEB_ROOT, 'node_modules', 'bittorrent-tracker', 'server.js'),
).href);

const descriptors = await candidateDescriptors();
const totalBytes = descriptors.reduce((sum, file) => sum + file.size_bytes, 0);
const tracker = new TrackerServer({ http: true, udp: false, ws: true, stats: false, interval: 1_000 });
let staticServer;
let seederChrome;
let fetcherChrome;
let seederPage;
let fetcherPage;
const requests = [];

try {
  const trackerPort = await listen(tracker);
  const localTracker = `ws://127.0.0.1:${trackerPort}`;
  staticServer = createServer(async (request, response) => {
    try {
      const url = new URL(request.url, 'http://127.0.0.1');
      requests.push(url.pathname);
      if (url.pathname === '/') {
        response.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' });
        response.end('<!doctype html><meta charset="utf-8"><title>VibApp RoomHash acceptance</title>');
        return;
      }
      const relativePath = decodeURIComponent(url.pathname).replace(/^\/+/, '');
      const absolutePath = resolve(WORKSPACE, relativePath);
      if (!absolutePath.startsWith(WORKSPACE + sep)) throw new Error('path outside workspace');
      const metadata = await stat(absolutePath);
      if (!metadata.isFile()) throw new Error('not a regular file');
      response.writeHead(200, { 'content-type': contentType(absolutePath), 'cache-control': 'no-store' });
      createReadStream(absolutePath).pipe(response);
    } catch {
      response.writeHead(404, { 'content-type': 'text/plain; charset=utf-8' });
      response.end('not found');
    }
  });
  const staticPort = await listen(staticServer);
  const origin = `http://127.0.0.1:${staticPort}`;
  const candidateUrlBase = '/' + relative(WORKSPACE, CANDIDATE_DIRECTORY).split(sep).join('/');

  [seederChrome, fetcherChrome] = await Promise.all([launchChrome(), launchChrome()]);
  progress('two isolated Chrome profiles and DevTools endpoints ready');
  [seederPage, fetcherPage] = await Promise.all([
    seederChrome.newPage('about:blank'),
    fetcherChrome.newPage('about:blank'),
  ]);
  await Promise.all([seederPage.navigate(origin), fetcherPage.navigate(origin)]);
  progress('two isolated browser nodes loaded');

  const seeded = await seederPage.evaluate(`(async () => {
    const { default: WebTorrent } = await import('/artifacts/web-client-core/vendor-roomhash/webtorrent.min.js');
    const tracker = ${JSON.stringify(localTracker)};
    const digest = ${JSON.stringify(PACKAGE_DIGEST)};
    const paths = ${JSON.stringify(CANDIDATE_PATHS)};
    const base = ${JSON.stringify(candidateUrlBase)};
    const events = [];
    const client = new WebTorrent({ dht: false, lsd: false, maxConns: 16, tracker: { announce: [tracker] } });
    const files = await Promise.all(paths.map(async path => {
      const response = await fetch(base + '/' + path);
      if (!response.ok) throw new Error('candidate source fetch failed: ' + path);
      return new File([await response.arrayBuffer()], path);
    }));
    const torrent = await new Promise((resolve, reject) => {
      const value = client.seed(files, {
        name: digest + '.vibapp-candidate',
        announce: [tracker],
      }, resolve);
      value.on('peer', peer => events.push({ event: 'peer', peer: String(peer).slice(0, 128) }));
      value.on('wire', (_wire, address) => events.push({ event: 'wire', peer: String(address).slice(0, 128) }));
      value.on('warning', error => events.push({ event: 'warning', message: String(error?.message || error).slice(0, 256) }));
      value.once('error', reject);
    });
    window.__vibappSeeder = { client, torrent, events };
    return {
      infoHash: torrent.infoHash,
      files: torrent.files.map(file => ({ path: file.path, sizeBytes: file.length })),
    };
  })()`);
  progress('real browser seeder announced the five-file candidate');
  if (!/^[0-9a-f]{40}$/.test(seeded.infoHash)) throw new Error('browser seeder returned an invalid info hash');

  const magnetUri = `magnet:?xt=urn:btih:${seeded.infoHash}&dn=${PACKAGE_DIGEST}.vibapp-candidate`
    + LOCATOR_TRACKERS.map(url => `&tr=${encodeURIComponent(url)}`).join('');
  const locator = validatePublicLocator({
    schema_version: 'vibapp.roomhash-package-locator.experimental-v1',
    package_digest_sha256: PACKAGE_DIGEST,
    transport: 'bittorrent-v1',
    info_hash: seeded.infoHash,
    magnet_uri: magnetUri,
    size_bytes: totalBytes,
    files: descriptors,
    trust_note: 'locator-only-package-bytes-require-vibapp-verification',
  }, PACKAGE_DIGEST);
  if (locator.magnet_uri.includes('127.0.0.1') || locator.magnet_uri.includes('ws%3A')) {
    throw new Error('test tracker leaked into the production locator');
  }
  progress('strict fixed-WSS production locator validation passed');

  const candidateRequestMark = requests.length;
  const fetched = await fetcherPage.evaluate(`(async () => {
    const locator = ${JSON.stringify(locator)};
    const fixedTrackers = ${JSON.stringify(FIXED_PUBLIC_TRACKERS)};
    const localTracker = ${JSON.stringify(localTracker)};
    const [{ createRoomHashBrowserNode }, { createBrowserPackageStore }, { default: WebTorrent }] = await Promise.all([
      import('/artifacts/web-client-core/roomhash-browser-node.mjs'),
      import('/artifacts/web-client-core/browser-package-store.mjs'),
      import('/artifacts/web-client-core/vendor-roomhash/webtorrent.min.js'),
    ]);
    const evidence = { peer: [], wire: [], warnings: [], clientErrors: [], addCalls: 0, mappedTrackers: [] };
    let wasmExecutions = 0;
    const originalInstantiate = WebAssembly.instantiate;
    const originalInstantiateStreaming = WebAssembly.instantiateStreaming;
    WebAssembly.instantiate = function (...args) {
      wasmExecutions += 1;
      return originalInstantiate.apply(this, args);
    };
    WebAssembly.instantiateStreaming = function (...args) {
      wasmExecutions += 1;
      return originalInstantiateStreaming.apply(this, args);
    };
    const store = createBrowserPackageStore({
      databaseName: 'vibapp-roomhash-acceptance-' + crypto.randomUUID(),
      cacheLimitBytes: 64 * 1024 * 1024,
    });
    const transportFactory = async options => {
      if (JSON.stringify(options?.tracker?.announce) !== JSON.stringify(fixedTrackers)) {
        throw new Error('production transport options did not retain the fixed WSS tracker allowlist');
      }
      const client = new WebTorrent({ ...options, tracker: { announce: [localTracker] } });
      client.on('error', error => evidence.clientErrors.push(String(error?.message || error).slice(0, 256)));
      window.__vibappFetcherClient = client;
      return new Proxy(client, {
        get(target, property) {
          if (property === 'add') {
            return (strictMagnet, addOptions, callback) => {
              evidence.addCalls += 1;
              if (strictMagnet !== locator.magnet_uri) throw new Error('transport received a locator other than the validated production magnet');
              const expectedTrackers = new URL(locator.magnet_uri).searchParams.getAll('tr');
              if (JSON.stringify(addOptions?.announce) !== JSON.stringify(expectedTrackers)) {
                throw new Error('transport received an unbound announce list');
              }
              const parsed = new URL(strictMagnet);
              const mapped = 'magnet:?xt=' + parsed.searchParams.get('xt')
                + '&dn=' + encodeURIComponent(parsed.searchParams.get('dn'))
                + '&tr=' + encodeURIComponent(localTracker);
              evidence.mappedTrackers.push(localTracker);
              const torrent = target.add(mapped, { ...addOptions, announce: [localTracker] }, callback);
              window.__vibappFetcherTorrent = torrent;
              torrent.on('peer', peer => evidence.peer.push(String(peer).slice(0, 128)));
              torrent.on('wire', (_wire, address) => evidence.wire.push(String(address).slice(0, 128)));
              torrent.on('warning', error => evidence.warnings.push(String(error?.message || error).slice(0, 256)));
              return torrent;
            };
          }
          const value = Reflect.get(target, property, target);
          return typeof value === 'function' ? value.bind(target) : value;
        },
      });
    };
    const node = createRoomHashBrowserNode({ transportFactory, candidateStore: store });
    window.__vibappFetcher = { node, store, evidence };
    try {
      await node.start({
        p2pEnabled: true,
        seedVerifiedApps: true,
        maxActiveTransfers: 4,
        uploadLimitKibPerSecond: 0,
        downloadLimitKibPerSecond: 0,
      });
      const receipt = await node.fetchVerifiedCandidateToStore({
        packageDigestSha256: locator.package_digest_sha256,
        infoHash: locator.info_hash,
        magnetURI: locator.magnet_uri,
        files: locator.files.map(file => ({ path: file.path, sha256: file.sha256, sizeBytes: file.size_bytes })),
        timeoutMs: 60_000,
      });
      const files = [];
      for (const descriptor of locator.files) {
        const bytes = await store.readFile(locator.package_digest_sha256, descriptor.path);
        const digest = await crypto.subtle.digest('SHA-256', bytes);
        const observedSha256 = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
        files.push({
          path: descriptor.path,
          sizeBytes: bytes.byteLength,
          expectedSha256: descriptor.sha256,
          observedSha256,
          verified: bytes.byteLength === descriptor.size_bytes && observedSha256 === descriptor.sha256,
        });
      }
      const torrent = window.__vibappFetcherTorrent;
      return {
        receipt,
        files,
        downloadedBytes: torrent?.downloaded || 0,
        numPeers: torrent?.numPeers || 0,
        events: evidence,
        wasmExecutions,
        publicResult: JSON.stringify(receipt),
      };
    } finally {
      await node.stop().catch(() => {});
      await store.close().catch(() => {});
      WebAssembly.instantiate = originalInstantiate;
      WebAssembly.instantiateStreaming = originalInstantiateStreaming;
    }
  })()`);
  progress('browser adapter committed the verified candidate to IndexedDB');

  const seederEvidence = await seederPage.evaluate(`(() => ({
    uploadedBytes: window.__vibappSeeder.torrent.uploaded,
    numPeers: window.__vibappSeeder.torrent.numPeers,
    events: window.__vibappSeeder.events,
  }))()`);
  await seederPage.evaluate(`new Promise(resolve => window.__vibappSeeder.client.destroy(resolve))`);

  const fetchPhaseRequests = requests.slice(candidateRequestMark);
  const candidateFetches = fetchPhaseRequests.filter(path => path.startsWith(candidateUrlBase + '/'));
  const forbiddenReceiptData = /magnet|infohash|info_hash|\bfiles\b|\bbytes\b/i.test(fetched.publicResult);
  const fileFailures = fetched.files.filter(file => !file.verified);
  const gates = {
    receipt_schema: fetched.receipt?.schemaVersion === 'vibapp.browser-package-cache-receipt.experimental-v1',
    receipt_state: fetched.receipt?.state === 'committed',
    receipt_digest: fetched.receipt?.packageDigestSha256 === PACKAGE_DIGEST,
    receipt_file_count: fetched.receipt?.fileCount === descriptors.length,
    receipt_size: fetched.receipt?.sizeBytes === totalBytes,
    exact_download_bytes: fetched.downloadedBytes === totalBytes,
    seeder_uploaded_candidate: seederEvidence.uploadedBytes >= totalBytes,
    one_transport_add: fetched.events.addCalls === 1,
    fetcher_peer: fetched.events.peer.length >= 1,
    fetcher_wire: fetched.events.wire.length >= 1,
    seeder_peer: seederEvidence.events.filter(event => event.event === 'peer').length >= 1,
    seeder_wire: seederEvidence.events.filter(event => event.event === 'wire').length >= 1,
    all_files_verified: fileFailures.length === 0,
    bounded_receipt: !forbiddenReceiptData,
    cached_wasm_not_executed: fetched.wasmExecutions === 0,
    no_http_candidate_fetch: candidateFetches.length === 0,
  };
  if (Object.values(gates).some(passed => !passed)) {
    throw new Error(`RoomHash public browser cache acceptance gates failed: ${JSON.stringify({
      gates,
      receipt: fetched.receipt,
      downloadedBytes: fetched.downloadedBytes,
      uploadedBytes: seederEvidence.uploadedBytes,
      fetcherEvents: fetched.events,
      seederEvents: seederEvidence.events,
      fileFailures,
      candidateFetches,
      wasmExecutions: fetched.wasmExecutions,
    })}`);
  }

  process.stdout.write(JSON.stringify({
    schema_version: 'vibapp.roomhash-public-browser-cache-acceptance.experimental-v1',
    state: 'pass',
    browser: {
      executable: seederChrome.executable,
      nodes: 2,
      isolated_profiles: 2,
      transport: 'real-vendored-webtorrent-webrtc',
    },
    locator: {
      validation: 'validatePublicLocator-pass-before-test-transport-mapping',
      package_digest_sha256: locator.package_digest_sha256,
      info_hash: locator.info_hash,
      fixed_wss_trackers: LOCATOR_TRACKERS,
      test_tracker_present: false,
    },
    test_transport: {
      isolated_local_tracker: true,
      mapped_only_after_locator_validation: true,
      add_calls: fetched.events.addCalls,
    },
    cache_receipt: fetched.receipt,
    transfer: {
      uploaded_bytes: seederEvidence.uploadedBytes,
      downloaded_bytes: fetched.downloadedBytes,
      expected_bytes: totalBytes,
      seeder_peer_events: seederEvidence.events.filter(event => event.event === 'peer').length,
      seeder_wire_events: seederEvidence.events.filter(event => event.event === 'wire').length,
      fetcher_peer_events: fetched.events.peer.length,
      fetcher_wire_events: fetched.events.wire.length,
    },
    file_verification: fetched.files,
    authority_boundary: {
      cache_receipt_contains_transport_or_bytes: forbiddenReceiptData,
      candidate_bytes_fetched_over_http_during_fetch: candidateFetches.length,
      cached_wasm_executions: fetched.wasmExecutions,
      installation_performed: false,
    },
  }, null, 2) + '\n');
} finally {
  try { seederPage?.close(); } catch {}
  try { fetcherPage?.close(); } catch {}
  await seederChrome?.close().catch(() => {});
  await fetcherChrome?.close().catch(() => {});
  await closeServer(staticServer).catch(() => {});
  await closeServer(tracker).catch(() => {});
}
