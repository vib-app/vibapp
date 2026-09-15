import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { delimiter, dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import test from 'node:test';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, '../../..');
const HOST = resolve(HERE, '../roomhash-host.mjs');
const ROOMHASH = resolve(ROOT, '../RoomHash');
const CANDIDATE_ROOT = resolve(ROOT, 'artifacts/app-builder/demo-output/pipeline/candidates');
const CANDIDATE = resolve(
  CANDIDATE_ROOT,
  'dfad1fed5fe0eb8c5bf83927eee64695287e22b5c8149d878b231d69d056e674/candidate.json',
);
const CONFIG = {
  upload_limit_bps: 1024 * 1024,
  download_limit_bps: 4 * 1024 * 1024,
  max_conns: 4,
  dht: false,
  lsd: false,
  pex: false,
  tracker_urls: [],
  turn: null,
};

async function launch(allowedRoots = [CANDIDATE_ROOT]) {
  const scratch = await mkdtemp(join(tmpdir(), 'vibapp-roomhash-host-'));
  const child = spawn(process.execPath, [HOST], {
    stdio: ['pipe', 'pipe', 'pipe'],
    env: {
      PATH: process.env.PATH || '',
      VIBAPP_ROOMHASH_ROOT: ROOMHASH,
      VIBAPP_ROOMHASH_DATA_DIR: scratch,
      VIBAPP_ROOMHASH_ALLOWED_SEED_ROOTS: allowedRoots.join(delimiter),
    },
  });
  let stderr = '';
  child.stderr.setEncoding('utf8');
  child.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-8192); });
  const lines = createInterface({ input: child.stdout });
  const waiting = [];
  const buffered = [];
  lines.on('line', line => {
    const parsed = JSON.parse(line);
    const next = waiting.shift();
    if (next) next(parsed);
    else buffered.push(parsed);
  });
  const next = (timeoutMs = 20_000) => new Promise((resolveNext, rejectNext) => {
    if (buffered.length) return resolveNext(buffered.shift());
    const timeout = setTimeout(() => rejectNext(new Error(`host response timeout: ${stderr}`)), timeoutMs);
    waiting.push(value => {
      clearTimeout(timeout);
      resolveNext(value);
    });
  });
  const ready = await next();
  let sequence = 0;
  async function send(command, args = {}) {
    sequence += 1;
    child.stdin.write(`${JSON.stringify({
      schema: 'vibapp.roomhash-host-request.experimental-v1',
      request_id: sequence,
      command,
      args,
    })}\n`);
    return next(command === 'start' ? 30_000 : 20_000);
  }
  async function close() {
    if (child.exitCode === null && !child.killed) {
      try { await send('shutdown'); } catch {}
    }
    child.stdin.end();
    await new Promise(resolveExit => {
      if (child.exitCode !== null) return resolveExit();
      const timeout = setTimeout(() => { child.kill('SIGKILL'); resolveExit(); }, 5_000);
      child.once('exit', () => { clearTimeout(timeout); resolveExit(); });
    });
    await rm(scratch, { recursive: true, force: true });
  }
  return { child, ready, send, close, scratch, stderr: () => stderr };
}

test('real RoomHash host completes ready/configure/start/status/shutdown lifecycle', async t => {
  const host = await launch();
  t.after(host.close);
  assert.deepEqual(host.ready, {
    schema: 'vibapp.roomhash-host-response.experimental-v1',
    request_id: 'ready',
    ok: true,
    result: { status: 'ready' },
    error: null,
  });
  const configured = await host.send('configure', CONFIG);
  assert.equal(configured.ok, true);
  assert.deepEqual(configured.result, { status: 'configured', turn_configured: false });
  const started = await host.send('start');
  assert.equal(started.ok, true, JSON.stringify(started));
  assert.equal(started.result.started, true);
  assert.equal(started.result.implementation, 'roomhash-current-headless-webtorrent-3.0.16');
  assert.equal(started.result.collaboration.available, true, JSON.stringify(started));
  assert.equal(started.result.collaboration.state, 'ready');
  assert.equal(started.result.collaboration.limits.max_sessions, 4);
  const status = await host.send('status');
  assert.equal(status.result.status, 'running');
  assert.equal(status.result.active_transfers, 0);
  assert.equal('client' in status.result, false);
  assert.equal('tracker_urls' in status.result, false);
});

test('host refuses a linked download root before announcing ready', async t => {
  const scratch = await mkdtemp(join(tmpdir(), 'vibapp-roomhash-linked-root-'));
  const outside = await mkdtemp(join(tmpdir(), 'vibapp-roomhash-linked-outside-'));
  t.after(() => rm(scratch, { recursive: true, force: true }));
  t.after(() => rm(outside, { recursive: true, force: true }));
  const sentinel = join(outside, 'sentinel.txt');
  await writeFile(sentinel, 'must-not-change');
  await symlink(outside, join(scratch, 'downloads'), 'dir');
  const child = spawn(process.execPath, [HOST], {
    stdio: ['ignore', 'pipe', 'pipe'],
    env: {
      PATH: process.env.PATH || '',
      VIBAPP_ROOMHASH_ROOT: ROOMHASH,
      VIBAPP_ROOMHASH_DATA_DIR: scratch,
      VIBAPP_ROOMHASH_ALLOWED_SEED_ROOTS: CANDIDATE_ROOT,
    },
  });
  t.after(() => { if (child.exitCode === null) child.kill('SIGKILL'); });
  let stdout = '';
  child.stdout.setEncoding('utf8');
  child.stdout.on('data', chunk => { stdout += chunk; });
  const exitCode = await new Promise((resolveExit, rejectExit) => {
    child.once('error', rejectExit);
    child.once('exit', resolveExit);
  });
  assert.notEqual(exitCode, 0);
  assert.equal(stdout, '');
  assert.equal(await readFile(sentinel, 'utf8'), 'must-not-change');
});

test('trusted collaboration broker requires exact one-time grants and bounded base64 messages', async t => {
  const host = await launch();
  t.after(host.close);
  assert.equal((await host.send('configure', CONFIG)).ok, true);
  assert.equal((await host.send('start')).ok, true);
  const channelId = '20000000-0000-4000-8000-000000000001';
  const grantId = '10000000-0000-4000-8000-000000000001';
  const expiresAt = Date.now() + 60_000;
  const grant = {
    schema: 'vibapp.collaboration-grant.experimental-v1',
    grant_id: grantId,
    approved: true,
    operation: 'create',
    channel_id: channelId,
    expires_at: expiresAt,
  };
  const nil = await host.send('collaboration-join', {
    channel_id: '00000000-0000-0000-0000-000000000000',
    expires_at: expiresAt,
    grant,
  });
  assert.equal(nil.ok, false);
  assert.equal(nil.error.code, 'invalid-request');

  const mismatched = await host.send('collaboration-create', {
    channel_id: channelId,
    expires_at: expiresAt,
    grant: { ...grant, channel_id: '20000000-0000-4000-8000-000000000002' },
  });
  assert.equal(mismatched.ok, false);
  assert.equal(mismatched.error.code, 'grant-denied');

  const created = await host.send('collaboration-create', {
    channel_id: channelId,
    expires_at: expiresAt,
    grant,
  });
  assert.equal(created.ok, true, JSON.stringify(created));
  assert.equal(created.result.channel_id, channelId);
  assert.match(created.result.session_id, /^[0-9a-f-]{36}$/);
  const sent = await host.send('collaboration-send', {
    session_id: created.result.session_id,
    message_base64: Buffer.from([1, 2, 3]).toString('base64'),
  });
  assert.deepEqual(sent.result, { session_id: created.result.session_id, accepted_bytes: 3 });
  const status = await host.send('collaboration-status');
  assert.equal(status.result.session_count, 1);
  assert.equal(status.result.sessions[0].sent_bytes, 3);
  assert.equal(status.result.queued_messages, 0);
  assert.deepEqual((await host.send('collaboration-receive', {
    session_id: created.result.session_id,
    limit: 4,
  })).result, { session_id: created.result.session_id, messages: [] });
  const excessiveReceive = await host.send('collaboration-receive', {
    session_id: created.result.session_id,
    limit: 5,
  });
  assert.equal(excessiveReceive.ok, false);
  assert.equal(excessiveReceive.error.code, 'invalid-request');

  const oversized = await host.send('collaboration-send', {
    session_id: created.result.session_id,
    message_base64: Buffer.alloc(64 * 1024 + 1).toString('base64'),
  });
  assert.equal(oversized.ok, false);
  assert.equal(oversized.error.code, 'resource-limit');
  const nonCanonical = await host.send('collaboration-send', {
    session_id: created.result.session_id,
    message_base64: 'AQI',
  });
  assert.equal(nonCanonical.ok, false);
  assert.equal(nonCanonical.error.code, 'invalid-request');

  const replay = await host.send('collaboration-create', {
    channel_id: channelId,
    expires_at: expiresAt,
    grant,
  });
  assert.equal(replay.ok, false);
  assert.equal(replay.error.code, 'grant-replayed');
  assert.equal((await host.send('collaboration-leave', {
    session_id: created.result.session_id,
  })).ok, true);
  const joinedChannel = '20000000-0000-4000-8000-000000000003';
  const joined = await host.send('collaboration-join', {
    channel_id: joinedChannel,
    expires_at: expiresAt,
    grant: {
      ...grant,
      grant_id: '10000000-0000-4000-8000-000000000002',
      operation: 'join',
      channel_id: joinedChannel,
    },
  });
  assert.equal(joined.ok, true, JSON.stringify(joined));
  assert.equal(joined.result.channel_id, joinedChannel);
  assert.equal((await host.send('collaboration-leave', {
    session_id: joined.result.session_id,
  })).ok, true);
  assert.equal((await host.send('collaboration-status')).result.session_count, 0);
});

test('verified candidate directory is seeded only after file-by-file integrity checks', async t => {
  const host = await launch();
  t.after(host.close);
  assert.equal((await host.send('configure', CONFIG)).ok, true);
  assert.equal((await host.send('start')).ok, true);
  const digest = 'dfad1fed5fe0eb8c5bf83927eee64695287e22b5c8149d878b231d69d056e674';
  const seeded = await host.send('seed-package', {
    candidate_path: CANDIDATE,
    package_digest_sha256: digest,
  });
  assert.equal(seeded.ok, true, JSON.stringify(seeded));
  assert.equal(seeded.result.package_digest_sha256, digest);
  assert.match(seeded.result.info_hash, /^[0-9a-f]{40}$/);
  assert.match(seeded.result.magnet_uri, /^magnet:\?xt=urn:btih:/);
  assert.deepEqual(
    seeded.result.files.map(item => item.path).sort(),
    [
      'candidate.json',
      'package/component.wasm',
      'package/manifest.json',
      'package/provenance.json',
      'package/sbom.cdx.json',
    ],
  );
  assert.equal((await host.send('status')).result.torrent_count, 1);
  const repeated = await host.send('seed-package', {
    candidate_path: CANDIDATE,
    package_digest_sha256: digest,
  });
  assert.deepEqual(repeated.result, seeded.result);
});

test('a second real WebTorrent node downloads the verified candidate from the host over TCP', async t => {
  const host = await launch();
  t.after(host.close);
  assert.equal((await host.send('configure', CONFIG)).ok, true);
  assert.equal((await host.send('start')).ok, true);
  const digest = 'dfad1fed5fe0eb8c5bf83927eee64695287e22b5c8149d878b231d69d056e674';
  const seeded = (await host.send('seed-package', {
    candidate_path: CANDIDATE,
    package_digest_sha256: digest,
  })).result;
  let port = (await host.send('status')).result.torrent_port;
  for (let attempt = 0; (!Number.isInteger(port) || port < 1) && attempt < 20; attempt += 1) {
    await new Promise(resolveWait => setTimeout(resolveWait, 25));
    port = (await host.send('status')).result.torrent_port;
  }
  assert.ok(Number.isInteger(port) && port > 0, `invalid torrent port: ${port}`);

  const moduleUrl = pathToFileURL(resolve(ROOMHASH, 'headless/node_modules/webtorrent/index.js')).href;
  const { default: WebTorrent } = await import(moduleUrl);
  const client = new WebTorrent({ dht: false, lsd: false, tracker: false, utp: false });
  t.after(() => Promise.resolve(client.destroy()).catch(() => {}));
  const downloadRoot = await mkdtemp(join(tmpdir(), 'vibapp-roomhash-download-'));
  t.after(() => rm(downloadRoot, { recursive: true, force: true }));
  const torrent = client.add(seeded.magnet_uri, { path: downloadRoot });
  const transferEvents = [];
  torrent.on('peer', peer => transferEvents.push(`peer:${String(peer)}`));
  torrent.on('wire', (_wire, address) => transferEvents.push(`wire:${address}`));
  torrent.on('warning', error => transferEvents.push(`warning:${error.message}`));
  torrent.on('noPeers', announceType => transferEvents.push(`no-peers:${announceType}`));
  torrent.on('metadata', () => transferEvents.push('metadata'));
  if (!torrent.infoHash) await new Promise(resolveInfoHash => torrent.once('infoHash', resolveInfoHash));
  transferEvents.push(`add-peer:${torrent.addPeer(`127.0.0.1:${port}`)}`);
  await new Promise((resolveDone, rejectDone) => {
    const timeout = setTimeout(() => rejectDone(new Error(`real torrent download timed out: ${transferEvents.join(',')}`)), 30_000);
    torrent.once('done', () => { clearTimeout(timeout); resolveDone(); });
    torrent.once('error', error => { clearTimeout(timeout); rejectDone(error); });
  });
  assert.equal(torrent.done, true);
  assert.equal(torrent.files.length, seeded.files.length);
  for (const expected of seeded.files) {
    const file = torrent.files.find(item => item.path === expected.path || item.path.endsWith(`/${expected.path}`));
    assert.ok(file, `missing ${expected.path}`);
    assert.equal(file.length, expected.size_bytes);
    const bytes = typeof file.arrayBuffer === 'function'
      ? Buffer.from(await file.arrayBuffer())
      : Buffer.from(await (await file.blob()).arrayBuffer());
    assert.equal((await import('node:crypto')).createHash('sha256').update(bytes).digest('hex'), expected.sha256);
  }
});

test('a second trusted RoomHash host fetches and re-verifies a candidate over local discovery', async t => {
  const seeder = await launch();
  const fetcher = await launch();
  t.after(seeder.close);
  t.after(fetcher.close);
  const localDiscovery = { ...CONFIG, lsd: true, pex: true };
  assert.equal((await seeder.send('configure', localDiscovery)).ok, true);
  assert.equal((await fetcher.send('configure', localDiscovery)).ok, true);
  assert.equal((await seeder.send('start')).ok, true);
  assert.equal((await fetcher.send('start')).ok, true);
  const digest = 'dfad1fed5fe0eb8c5bf83927eee64695287e22b5c8149d878b231d69d056e674';
  const seeded = (await seeder.send('seed-package', {
    candidate_path: CANDIDATE,
    package_digest_sha256: digest,
  })).result;
  const fetched = await fetcher.send('fetch', {
    magnet_uri: seeded.magnet_uri,
    package_digest_sha256: digest,
    files: seeded.files,
    timeout_ms: 30_000,
  });
  assert.equal(fetched.ok, true, JSON.stringify(fetched));
  assert.equal(fetched.result.operation, 'fetch');
  assert.equal(fetched.result.package_digest_sha256, digest);
  assert.equal(fetched.result.info_hash, seeded.info_hash);
  assert.equal(fetched.result.size_bytes, seeded.size_bytes);
  assert.match(fetched.result.candidate_path, /candidate\.json$/);
  assert.deepEqual(
    JSON.parse(await readFile(fetched.result.candidate_path, 'utf8')),
    JSON.parse(await readFile(CANDIDATE, 'utf8')),
  );
  const repeated = await fetcher.send('fetch', {
    magnet_uri: seeded.magnet_uri,
    package_digest_sha256: digest,
    files: seeded.files,
    timeout_ms: 30_000,
  });
  assert.equal(repeated.ok, true, JSON.stringify(repeated));
  assert.deepEqual(repeated.result, fetched.result);
});

test('fetch rejects a linked digest destination before WebTorrent can write outside managed storage', async t => {
  const fetcher = await launch();
  t.after(fetcher.close);
  assert.equal((await fetcher.send('configure', CONFIG)).ok, true);
  assert.equal((await fetcher.send('start')).ok, true);
  const digest = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
  const infoHash = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';
  const outside = await mkdtemp(join(tmpdir(), 'vibapp-roomhash-outside-'));
  t.after(() => rm(outside, { recursive: true, force: true }));
  const sentinel = join(outside, 'sentinel.txt');
  await writeFile(sentinel, 'must-not-change');
  await symlink(outside, join(fetcher.scratch, 'downloads', digest), 'dir');

  const fetched = await fetcher.send('fetch', {
    magnet_uri: `magnet:?xt=urn:btih:${infoHash}&dn=${digest}.vibapp-candidate`,
    package_digest_sha256: digest,
    files: [
      { path: 'candidate.json', sha256: 'c'.repeat(64), size_bytes: 1 },
      { path: 'package/manifest.json', sha256: 'd'.repeat(64), size_bytes: 1 },
    ],
    timeout_ms: 1_000,
  });
  assert.equal(fetched.ok, false);
  assert.equal(fetched.error.code, 'path-denied');
  assert.equal(await readFile(sentinel, 'utf8'), 'must-not-change');
});

test('unsupported RTC TURN is reported without blocking torrent startup or reflecting its credential', async t => {
  const host = await launch();
  t.after(host.close);
  const credential = 'do-not-reflect-this-turn-secret';
  const configured = await host.send('configure', {
    ...CONFIG,
    turn: { urls: ['turns:turn.example.invalid:5349?transport=tcp'], username: 'vibapp', credential },
  });
  assert.equal(configured.ok, true);
  assert.doesNotMatch(JSON.stringify(configured), new RegExp(credential));
  const started = await host.send('start');
  assert.equal(started.ok, true, JSON.stringify(started));
  assert.equal(started.result.started, true);
  assert.equal(started.result.collaboration.available, false);
  assert.equal(started.result.collaboration.reason_code, 'turn-unsupported');
  assert.equal(started.result.collaboration.turn_configured, true);
  assert.equal(started.result.collaboration.turn_applied, false);
  assert.doesNotMatch(JSON.stringify(started), new RegExp(credential));
  const source = await readFile(HOST, 'utf8');
  assert.doesNotMatch(source, /mesh-node|crawlDiscoveredChannels|channelsEnvelope/);
});
