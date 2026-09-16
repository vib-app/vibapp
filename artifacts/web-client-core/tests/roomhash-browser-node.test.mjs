import assert from 'node:assert/strict';
import { createHash, webcrypto } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { createRoomHashBrowserNode } from '../roomhash-browser-node.mjs';

if (!globalThis.crypto) globalThis.crypto = webcrypto;

const INFO_HASH = '0123456789abcdef0123456789abcdef01234567';
const MAGNET = `magnet:?xt=urn:btih:${INFO_HASH}`;
const digest = bytes => createHash('sha256').update(bytes).digest('hex');
const candidateMagnet = packageDigestSha256 => (
  `magnet:?xt=urn:btih:${INFO_HASH}`
  + `&dn=${packageDigestSha256}.vibapp-candidate`
  + '&tr=wss%3A%2F%2Ftracker.webtorrent.dev'
  + '&tr=wss%3A%2F%2Ftracker.openwebtorrent.com'
);

function fakeFactory(downloadBytes = new TextEncoder().encode('downloaded-package')) {
  const calls = { factory: [], seed: [], add: [], remove: [], destroy: 0 };
  const torrent = {
    done: true,
    infoHash: INFO_HASH,
    magnetURI: MAGNET,
    files: [{
      length: downloadBytes.byteLength,
      async arrayBuffer() { return downloadBytes.slice().buffer; },
    }],
  };
  const factory = async options => {
    calls.factory.push(options);
    return {
      torrents: [],
      uploadSpeed: 12,
      downloadSpeed: 34,
      seed(bytes, options, callback) {
        calls.seed.push({ bytes, options });
        this.torrents.push(torrent);
        queueMicrotask(() => callback(torrent));
      },
      add(magnetURI, options, callback) {
        calls.add.push({ magnetURI, options });
        this.torrents.push(torrent);
        queueMicrotask(() => callback(torrent));
      },
      remove(infoHash, callback) {
        calls.remove.push(infoHash);
        queueMicrotask(callback);
      },
      destroy(callback) {
        calls.destroy += 1;
        queueMicrotask(callback);
      },
    };
  };
  return { calls, factory };
}

function candidateFixture({ invalidAuthority = false } = {}) {
  const encode = value => new TextEncoder().encode(typeof value === 'string' ? value : JSON.stringify(value));
  const component = encode('component');
  const provenance = encode({ source: 'verified' });
  const sbom = encode({ packages: [] });
  const documentFile = (path, bytes) => ({ path, sha256: digest(bytes), size_bytes: bytes.byteLength });
  const manifest = encode({
    artifacts: {
      canonical_component: documentFile('component.wasm', component),
      assets: [],
      provenance: documentFile('provenance.json', provenance),
      sbom: documentFile('sbom.json', sbom),
      browser_derivations: [],
    },
  });
  const packageDigestSha256 = 'a'.repeat(64);
  const candidate = encode({
    schema_version: 'vibapp.builder-candidate.experimental-v1',
    document_type: 'verifier-promoted-candidate',
    state: 'candidate-ready',
    package_directory: 'package',
    package_digest_sha256: packageDigestSha256,
    authority: { install: 'daemon', publish: 'none' },
    verification: {
      authority: invalidAuthority ? 'self-certified' : 'independent-verifier',
      checks: [{ outcome: 'pass' }],
    },
    manifest: documentFile('manifest.json', manifest),
    component: documentFile('component.wasm', component),
  });
  const entries = [
    ['candidate.json', candidate],
    ['package/manifest.json', manifest],
    ['package/component.wasm', component],
    ['package/provenance.json', provenance],
    ['package/sbom.json', sbom],
  ].map(([path, bytes]) => ({ path, bytes, sha256: digest(bytes), sizeBytes: bytes.byteLength }));
  return { packageDigestSha256, entries };
}

function fakeCandidateFactory(entries, { infoHash = INFO_HASH } = {}) {
  const calls = { add: 0, remove: [] };
  const torrent = {
    done: true,
    infoHash,
    magnetURI: MAGNET,
    files: entries.map(entry => ({
      path: `candidate-root/${entry.path}`,
      length: entry.bytes.byteLength,
      async arrayBuffer() { return entry.bytes.slice().buffer; },
    })),
  };
  return {
    calls,
    factory: async () => ({
      torrents: [],
      seed() {},
      add(_magnet, _options, callback) {
        calls.add += 1;
        queueMicrotask(() => callback(torrent));
      },
      remove(infoHash, callback) {
        calls.remove.push(infoHash);
        queueMicrotask(callback);
      },
      destroy(callback) { queueMicrotask(callback); },
    }),
  };
}

test('start and stop are idempotent and pass only bounded host-owned transport options', async () => {
  const fake = fakeFactory();
  const node = createRoomHashBrowserNode({ transportFactory: fake.factory });
  assert.equal(node.status().state, 'stopped');
  const settings = {
    p2pEnabled: true,
    seedVerifiedApps: true,
    maxActiveTransfers: 3,
    uploadLimitKibPerSecond: 10,
    downloadLimitKibPerSecond: 20,
    turnCredential: 'must-not-cross-the-adapter',
  };
  const [left, right] = await Promise.all([node.start(settings), node.start(settings)]);
  assert.equal(left.state, 'running');
  assert.equal(right.state, 'running');
  assert.equal(fake.calls.factory.length, 1);
  assert.deepEqual(fake.calls.factory[0], {
    dht: false,
    lsd: false,
    maxConns: 12,
    uploadLimit: 10 * 1024,
    downloadLimit: 20 * 1024,
    tracker: { announce: [
      'wss://tracker.webtorrent.dev',
      'wss://tracker.openwebtorrent.com',
      'wss://tracker.btorrent.xyz',
    ] },
  });
  assert.doesNotMatch(JSON.stringify(fake.calls.factory), /must-not-cross/);
  await assert.rejects(node.start({ ...settings, maxActiveTransfers: 4 }), /already-started-with-different-settings/);
  const [stoppedA, stoppedB] = await Promise.all([node.stop(), node.stop()]);
  assert.equal(stoppedA.state, 'stopped');
  assert.equal(stoppedB.state, 'stopped');
  assert.equal(fake.calls.destroy, 1);
  assert.equal((await node.stop()).state, 'stopped');
});

test('zero bandwidth settings use the WebTorrent unlimited sentinel instead of a zero-byte throttle', async () => {
  const fake = fakeFactory();
  const node = createRoomHashBrowserNode({ transportFactory: fake.factory });
  await node.start({ p2pEnabled: true });
  assert.equal(fake.calls.factory[0].uploadLimit, -1);
  assert.equal(fake.calls.factory[0].downloadLimit, -1);
  await node.stop();
});

test('seed accepts only pre-hashed bounded package bytes and deduplicates by SHA-256', async () => {
  const fake = fakeFactory();
  const node = createRoomHashBrowserNode({ transportFactory: fake.factory });
  await node.start({ p2pEnabled: true, seedVerifiedApps: true });
  const bytes = new TextEncoder().encode('verified-package');
  const input = { bytes, sha256: digest(bytes), name: 'package.vibapp' };
  const [first, second] = await Promise.all([node.seedVerifiedPackage(input), node.seedVerifiedPackage(input)]);
  assert.deepEqual(first, second);
  assert.equal(first.operation, 'seed');
  assert.equal(first.infoHash, INFO_HASH);
  assert.equal(first.sizeBytes, bytes.byteLength);
  assert.equal(fake.calls.seed.length, 1);
  await assert.rejects(
    node.seedVerifiedPackage({ ...input, sha256: '0'.repeat(64) }),
    /package-digest-mismatch/,
  );
  await assert.rejects(
    node.seedVerifiedPackage({ ...input, name: '../escape' }),
    /package-name/,
  );
  await node.stop();
});

test('fetch returns a copy only after exact size and SHA-256 verification', async () => {
  const bytes = new TextEncoder().encode('downloaded-package');
  const fake = fakeFactory(bytes);
  const node = createRoomHashBrowserNode({ transportFactory: fake.factory });
  await node.start({ p2pEnabled: true });
  const input = { magnetURI: MAGNET, sha256: digest(bytes), sizeBytes: bytes.byteLength, timeoutMs: 1_000 };
  const result = await node.fetchVerifiedPackage(input);
  assert.equal(result.operation, 'fetch');
  assert.deepEqual(result.bytes, bytes);
  result.bytes[0] ^= 0xff;
  const repeated = await node.fetchVerifiedPackage(input);
  assert.deepEqual(repeated.bytes, bytes);
  assert.equal(fake.calls.add.length, 2, 'completed downloads must not retain package bytes in a cache');
  assert.equal(node.status().activeTransfers, 0);
  await node.stop();
});

test('aggregate in-flight package bytes are bounded independently of transfer count', async () => {
  const callbacks = [];
  const node = createRoomHashBrowserNode({
    transportFactory: async () => ({
      torrents: [],
      seed() {},
      add(_magnet, _options, callback) { callbacks.push(callback); },
      destroy(callback) { callback(); },
    }),
  });
  await node.start({ p2pEnabled: true, maxActiveTransfers: 4 });
  const large = 64 * 1024 * 1024;
  void node.fetchVerifiedPackage({ magnetURI: MAGNET + '&x=1', sha256: '1'.repeat(64), sizeBytes: large });
  void node.fetchVerifiedPackage({ magnetURI: MAGNET + '&x=2', sha256: '2'.repeat(64), sizeBytes: large });
  await assert.rejects(node.fetchVerifiedPackage({
    magnetURI: MAGNET + '&x=3',
    sha256: '3'.repeat(64),
    sizeBytes: 1,
  }), /in-flight-byte-limit-reached/);
  assert.equal(callbacks.length, 2);
  assert.equal(node.status().inFlightBytes, 128 * 1024 * 1024);
});

test('fetch fails closed and removes torrent state after package tamper', async () => {
  const bytes = new TextEncoder().encode('tampered-package');
  const fake = fakeFactory(bytes);
  const node = createRoomHashBrowserNode({ transportFactory: fake.factory });
  await node.start({ p2pEnabled: true });
  await assert.rejects(node.fetchVerifiedPackage({
    magnetURI: MAGNET,
    sha256: 'f'.repeat(64),
    sizeBytes: bytes.byteLength,
  }), /package-digest-mismatch/);
  assert.deepEqual(fake.calls.remove, [INFO_HASH]);
  assert.match(node.status().lastError, /package-digest-mismatch/);
  await node.stop();
});

test('multi-file candidate fetch verifies every byte and independent verifier authority', async () => {
  const fixture = candidateFixture();
  const fake = fakeCandidateFactory(fixture.entries);
  const node = createRoomHashBrowserNode({ transportFactory: fake.factory });
  await node.start({ p2pEnabled: true });
  const result = await node.fetchVerifiedCandidate({
    magnetURI: candidateMagnet(fixture.packageDigestSha256),
    infoHash: INFO_HASH,
    packageDigestSha256: fixture.packageDigestSha256,
    files: fixture.entries.map(({ path, sha256, sizeBytes }) => ({ path, sha256, sizeBytes })),
    timeoutMs: 1_000,
  });
  assert.equal(result.operation, 'fetch-candidate');
  assert.equal(result.packageDigestSha256, fixture.packageDigestSha256);
  assert.deepEqual(result.files.map(file => file.path), fixture.entries.map(file => file.path));
  assert.equal(fake.calls.add, 1);
  assert.deepEqual(fake.calls.remove, []);
  await node.stop();
});

test('multi-file candidate fetch rejects self-certified metadata and drops torrent state', async () => {
  const fixture = candidateFixture({ invalidAuthority: true });
  const fake = fakeCandidateFactory(fixture.entries);
  const node = createRoomHashBrowserNode({ transportFactory: fake.factory });
  await node.start({ p2pEnabled: true });
  await assert.rejects(node.fetchVerifiedCandidate({
    magnetURI: candidateMagnet(fixture.packageDigestSha256),
    infoHash: INFO_HASH,
    packageDigestSha256: fixture.packageDigestSha256,
    files: fixture.entries.map(({ path, sha256, sizeBytes }) => ({ path, sha256, sizeBytes })),
  }), /candidate-verification-authority/);
  assert.deepEqual(fake.calls.remove, [INFO_HASH]);
  await node.stop();
});

test('candidate locator binds exact info hash, package name, and fixed WSS trackers', async () => {
  const fixture = candidateFixture();
  const fake = fakeCandidateFactory(fixture.entries);
  const node = createRoomHashBrowserNode({ transportFactory: fake.factory });
  const base = {
    infoHash: INFO_HASH,
    magnetURI: candidateMagnet(fixture.packageDigestSha256),
    packageDigestSha256: fixture.packageDigestSha256,
    files: fixture.entries.map(({ path, sha256, sizeBytes }) => ({ path, sha256, sizeBytes })),
  };
  await assert.rejects(node.fetchVerifiedCandidate({ ...base, infoHash: undefined }), /candidate-infohash/);
  await assert.rejects(node.fetchVerifiedCandidate({ ...base, infoHash: INFO_HASH.toUpperCase() }), /candidate-infohash/);
  await assert.rejects(node.fetchVerifiedCandidate({ ...base, trackerOverride: 'wss://evil.example' }), /candidate-locator-fields/);
  await assert.rejects(node.fetchVerifiedCandidate({
    ...base,
    magnetURI: base.magnetURI + '&xs=https%3A%2F%2Fexample.com%2Fcandidate',
  }), /candidate-magnet-parameter/);
  await assert.rejects(node.fetchVerifiedCandidate({
    ...base,
    magnetURI: base.magnetURI + '&tr=wss%3A%2F%2Fevil.example',
  }), /candidate-magnet-tracker/);
  await assert.rejects(node.fetchVerifiedCandidate({
    ...base,
    magnetURI: base.magnetURI.replace('.vibapp-candidate', '.wrong-name'),
  }), /candidate-magnet-binding/);
  await assert.rejects(node.fetchVerifiedCandidate({
    ...base,
    magnetURI: base.magnetURI + `&dn=${fixture.packageDigestSha256}.vibapp-candidate`,
  }), /candidate-magnet-binding/);
});

test('candidate torrent identity must equal the locator before bytes are accepted', async () => {
  const fixture = candidateFixture();
  const otherInfoHash = 'f'.repeat(40);
  const fake = fakeCandidateFactory(fixture.entries, { infoHash: otherInfoHash });
  const node = createRoomHashBrowserNode({ transportFactory: fake.factory });
  await node.start({ p2pEnabled: true });
  await assert.rejects(node.fetchVerifiedCandidate({
    infoHash: INFO_HASH,
    magnetURI: candidateMagnet(fixture.packageDigestSha256),
    packageDigestSha256: fixture.packageDigestSha256,
    files: fixture.entries.map(({ path, sha256, sizeBytes }) => ({ path, sha256, sizeBytes })),
  }), /candidate-infohash-mismatch/);
  assert.deepEqual(fake.calls.remove, [otherInfoHash]);
  await node.stop();
});

test('trusted store fetch stages verified files one at a time and returns no bytes or magnet', async () => {
  const fixture = candidateFixture();
  const fake = fakeCandidateFactory(fixture.entries);
  const writes = [];
  let committed = false;
  let aborted = false;
  const receipt = Object.freeze({
    schemaVersion: 'vibapp.browser-package-cache-receipt.experimental-v1',
    packageDigestSha256: fixture.packageDigestSha256,
    state: 'committed',
    fileCount: fixture.entries.length,
    sizeBytes: fixture.entries.reduce((sum, entry) => sum + entry.sizeBytes, 0),
    committedAtUnixMs: 123,
  });
  const untrustedStoreReceipt = {
    ...receipt,
    magnetURI: 'magnet:?must-not-return',
    files: fixture.entries,
    bytes: new Uint8Array([1, 2, 3]),
  };
  const candidateStore = {
    async begin(input) {
      assert.equal(input.packageDigestSha256, fixture.packageDigestSha256);
      return {
        alreadyCommitted: false,
        async writeFile(file) {
          assert.equal(committed, false);
          writes.push({ path: file.path, sizeBytes: file.bytes.byteLength });
        },
        async commit() {
          assert.equal(writes.length, fixture.entries.length);
          committed = true;
          return untrustedStoreReceipt;
        },
        async abort() { aborted = true; },
      };
    },
  };
  const node = createRoomHashBrowserNode({ transportFactory: fake.factory, candidateStore });
  await node.start({ p2pEnabled: true });
  const result = await node.fetchVerifiedCandidateToStore({
    infoHash: INFO_HASH,
    magnetURI: candidateMagnet(fixture.packageDigestSha256),
    packageDigestSha256: fixture.packageDigestSha256,
    files: fixture.entries.map(({ path, sha256, sizeBytes }) => ({ path, sha256, sizeBytes })),
  });
  assert.deepEqual(result, receipt);
  assert.deepEqual(writes.map(item => item.path), fixture.entries.map(item => item.path));
  assert.equal(aborted, false);
  assert.doesNotMatch(JSON.stringify(result), /magnet|"bytes"|"files"|blob:|data:/i);
  assert.equal(node.status().inFlightBytes, 0);
  await node.stop();
});

test('module has no Service Worker, ambient app global, or raw transport export', async () => {
  const source = await readFile(new URL('../roomhash-browser-node.mjs', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /serviceWorker|navigator\.|window\.|document\.|VibApp|RoomHash\s*=/);
  assert.match(source, /import\('\.\/vendor-roomhash\/webtorrent\.min\.js'\)/);
  const fake = fakeFactory();
  const node = createRoomHashBrowserNode({ transportFactory: fake.factory });
  assert.deepEqual(Object.keys(node).sort(), [
    'fetchHttpCandidateToStore',
    'fetchVerifiedCandidate',
    'fetchVerifiedCandidateToStore',
    'fetchVerifiedPackage',
    'seedVerifiedPackage',
    'start',
    'status',
    'stop',
  ]);
  assert.equal('client' in node, false);
});

test('HTTP acquisition works without starting P2P, is bounded and can be cancelled', async () => {
  const fixture = candidateFixture();
  const files = fixture.entries.map(({ path, sha256, sizeBytes }) => ({ path, sha256, sizeBytes }));
  const total = files.reduce((sum, file) => sum + file.sizeBytes, 0);
  let aborted = 0, committed = 0, starts = 0;
  const store = { async begin() { return {
    async writeFile(file) { assert.equal(digest(file.bytes), file.sha256); },
    async commit() { committed++; return { schemaVersion: 'vibapp.browser-package-cache-receipt.experimental-v1', packageDigestSha256: fixture.packageDigestSha256, state: 'committed', fileCount: files.length, sizeBytes: total, committedAtUnixMs: 1 }; },
    async abort() { aborted++; },
  }; } };
  const node = createRoomHashBrowserNode({ candidateStore: store, transportFactory: () => { starts++; throw new Error('P2P must stay off'); } });
  const base = `https://raw.githubusercontent.com/vib-app/packages/${'b'.repeat(40)}/packages/${fixture.packageDigestSha256}.vibapp-candidate/`;
  const input = { packageDigestSha256: fixture.packageDigestSha256, infoHash: INFO_HASH, magnetURI: candidateMagnet(fixture.packageDigestSha256), httpBase: base, files };
  const originalFetch = globalThis.fetch;
  try {
    let progress = 0;
    globalThis.fetch = async url => new Response(fixture.entries.find(file => url === base + file.path).bytes);
    await node.fetchHttpCandidateToStore({ ...input, onProgress: n => { progress = n; } });
    assert.equal(progress, total); assert.equal(starts, 0); assert.equal(committed, 1);
    const abort = new AbortController(); abort.abort();
    await assert.rejects(node.fetchHttpCandidateToStore({ ...input, signal: abort.signal }), /abort/i);
    globalThis.fetch = async () => new Response(new Uint8Array(total + 1));
    await assert.rejects(node.fetchHttpCandidateToStore(input), /size-mismatch/);
    await assert.rejects(node.fetchHttpCandidateToStore({ ...input, httpBase: 'https://evil.example/' }), /http-origin/);
    assert.equal(aborted, 2); assert.equal(committed, 1);
  } finally { globalThis.fetch = originalFetch; }
});

test('cancel while waiting for torrent metadata aborts staging and removes the pending torrent', async () => {
  const fixture = candidateFixture();
  const abort = new AbortController();
  let discarded = 0, removed = 0;
  const node = createRoomHashBrowserNode({
    candidateStore: { async begin() { return { async writeFile() {}, async commit() {}, async abort() { discarded++; } }; } },
    transportFactory: async () => ({
      seed() { throw new Error('Not part of metadata cancellation'); },
      add() { queueMicrotask(() => abort.abort()); return { infoHash: INFO_HASH }; },
      remove(hash, callback) { assert.equal(hash, INFO_HASH); removed++; callback(); },
      destroy(callback) { callback(); },
    }),
  });
  await node.start({ p2pEnabled: true });
  await assert.rejects(node.fetchVerifiedCandidateToStore({
    infoHash: INFO_HASH, magnetURI: candidateMagnet(fixture.packageDigestSha256),
    packageDigestSha256: fixture.packageDigestSha256,
    files: fixture.entries.map(({ path, sha256, sizeBytes }) => ({ path, sha256, sizeBytes })), signal: abort.signal,
  }), /cancel/i);
  assert.equal(discarded, 1); assert.equal(removed, 1); assert.equal(node.status().activeTransfers, 0);
  await node.stop();
});

test('sync emits exact adapter and vendored bundle digests for cache binding', async () => {
  const sync = new URL('../sync-roomhash-browser.mjs', import.meta.url);
  const result = spawnSync(process.execPath, [sync.pathname], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr || result.stdout);
  const generated = new URL('../../product-platform/website/public/roomhash/', import.meta.url);
  const manifest = JSON.parse(await readFile(new URL('SYNC.json', generated), 'utf8'));
  const adapter = await readFile(new URL('../roomhash-browser-node.mjs', import.meta.url));
  const packageStore = await readFile(new URL('../browser-package-store.mjs', import.meta.url));
  const bundle = await readFile(new URL('../vendor-roomhash/webtorrent.min.js', import.meta.url));
  assert.equal(manifest.schema_version, 'vibapp.roomhash-browser-sync.experimental-v1');
  assert.equal(manifest.adapter_sha256, digest(adapter));
  assert.equal(manifest.package_store_sha256, digest(packageStore));
  assert.equal(manifest.bundle_sha256, digest(bundle));
  assert.deepEqual(await readFile(new URL('roomhash-browser-node.mjs', generated)), adapter);
  assert.deepEqual(await readFile(new URL('browser-package-store.mjs', generated)), packageStore);
  assert.deepEqual(await readFile(new URL('vendor-roomhash/webtorrent.min.js', generated)), bundle);
});
