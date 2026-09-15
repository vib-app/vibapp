import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, mkdir, readFile, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import {
  applyVendorPatch, patchWebseedHeaders, ORIGINAL_HEADERS, PATCHED_HEADERS,
  UPSTREAM_SHA256, PATCHED_SHA256, UPSTREAM_SIZE, PATCHED_SIZE,
} from '../patch-roomhash-webseed-headers.mjs';

const vendorUrl = new URL('../vendor-roomhash/', import.meta.url);
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
const bundle = await readFile(new URL('webtorrent.min.js', vendorUrl));
const source = Buffer.from(bundle.toString('utf8').replace(PATCHED_HEADERS, ORIGINAL_HEADERS));
assert.equal(sha256(source), UPSTREAM_SHA256, 'inverse delta must recover exactly the pinned upstream bytes');
const provenance = JSON.parse(await readFile(new URL('PROVENANCE.json', vendorUrl), 'utf8'));

test('one exact browser webseed header delta reproduces the pinned vendored bundle', () => {
  const before = Buffer.from(source);
  const patched = patchWebseedHeaders(source);
  assert.deepEqual(source, before, 'patch must not mutate the upstream input buffer');
  assert.deepEqual(patched, bundle);
  assert.equal(source.length, UPSTREAM_SIZE);
  assert.equal(patched.length, PATCHED_SIZE);
  assert.equal(sha256(patched), PATCHED_SHA256);
  assert.equal(source.toString().split(ORIGINAL_HEADERS).length, 2);
  assert.equal(patched.toString().split(PATCHED_HEADERS).length, 2);
  assert.equal(patched.toString().includes(ORIGINAL_HEADERS), false);
  const start = source.indexOf(ORIGINAL_HEADERS);
  assert.deepEqual(patched.subarray(0, start), source.subarray(0, start));
  assert.deepEqual(patched.subarray(start + Buffer.byteLength(PATCHED_HEADERS)),
    source.subarray(start + Buffer.byteLength(ORIGINAL_HEADERS)));
});

test('cache policy, range, GET and AbortSignal survive without browser global mutation', () => {
  const expected = 'Re(r,{cache:"no-store",method:"GET",headers:{range:`bytes=${n}-${s}`},signal:AbortSignal.timeout(6e4)})';
  assert.equal(bundle.toString().split(expected).length, 2);
  assert.ok(bundle.subarray(-256).toString().includes('export{Qi as default}'));
  assert.deepEqual(provenance.local_patches[0].removed_request_headers, ['Cache-Control', 'user-agent']);
  assert.deepEqual(provenance.local_patches[0].preserved_request_options,
    ['cache:no-store', 'method:GET', 'range:bytes=${n}-${s}', 'signal:AbortSignal.timeout(60000)']);
});

test('patch rejects modified, missing, duplicated or already patched upstream bytes', () => {
  const variants = [
    Buffer.from(source.toString().replace('cache:"no-store"', 'cache:"default"')),
    Buffer.from(source.toString().replace(ORIGINAL_HEADERS, 'headers:{}')),
    Buffer.concat([source, Buffer.from(ORIGINAL_HEADERS)]),
    Buffer.from(bundle),
    Buffer.from(source.subarray(1)),
    source.toString(),
  ];
  for (const variant of variants) assert.throws(() => patchWebseedHeaders(variant), /hash\/size mismatch/);
});

test('upstream NPM proof and exact input/output/script hashes are retained', async () => {
  assert.equal(provenance.upstream_repository, 'https://github.com/webtorrent/webtorrent');
  assert.equal(provenance.resolved_package, 'https://registry.npmmirror.com/webtorrent/-/webtorrent-3.0.16.tgz');
  assert.equal(provenance.upstream_bundle.sha256, UPSTREAM_SHA256);
  assert.equal(provenance.upstream_bundle.size_bytes, UPSTREAM_SIZE);
  assert.equal(provenance.bundle.sha256, PATCHED_SHA256);
  assert.equal(provenance.bundle.size_bytes, PATCHED_SIZE);
  assert.equal(provenance.local_patches.length, 1);
  const patch = provenance.local_patches[0];
  assert.equal(patch.occurrence_count, 1);
  assert.equal(patch.input_sha256, UPSTREAM_SHA256);
  assert.equal(patch.output_sha256, PATCHED_SHA256);
  assert.equal(patch.script_sha256, sha256(await readFile(new URL('../patch-roomhash-webseed-headers.mjs', import.meta.url))));
});

async function fixture(run) {
  const root = await mkdtemp(join(tmpdir(), 'vibapp-webseed-patch-'));
  try {
    const vendor = join(root, 'vendor');
    const upstream = join(root, 'upstream.js');
    await mkdir(vendor);
    await writeFile(upstream, source);
    await writeFile(join(vendor, 'webtorrent.min.js'), source);
    const originalProvenance = structuredClone(provenance);
    delete originalProvenance.local_patches;
    delete originalProvenance.upstream_bundle;
    originalProvenance.bundle.sha256 = UPSTREAM_SHA256;
    originalProvenance.bundle.size_bytes = UPSTREAM_SIZE;
    await writeFile(join(vendor, 'PROVENANCE.json'), JSON.stringify(originalProvenance));
    await run({ source: upstream, vendor }, originalProvenance);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

test('mechanical patch command is idempotent and never rewrites upstream', async () => {
  await fixture(async options => {
    const before = await readFile(options.source);
    const first = await applyVendorPatch(options);
    const firstProvenance = await readFile(join(options.vendor, 'PROVENANCE.json'));
    assert.deepEqual(await readFile(join(options.vendor, 'webtorrent.min.js')), bundle);
    assert.deepEqual(await applyVendorPatch(options), first);
    assert.deepEqual(await readFile(join(options.vendor, 'PROVENANCE.json')), firstProvenance);
    assert.deepEqual(await readFile(options.source), before);
  });
});

test('unknown destination bytes fail before either file is overwritten', async () => {
  await fixture(async options => {
    const changed = Buffer.from('unreviewed local modification');
    await writeFile(join(options.vendor, 'webtorrent.min.js'), changed);
    const metadata = await readFile(join(options.vendor, 'PROVENANCE.json'));
    await assert.rejects(applyVendorPatch(options), /unreviewed local changes/);
    assert.deepEqual(await readFile(join(options.vendor, 'webtorrent.min.js')), changed);
    assert.deepEqual(await readFile(join(options.vendor, 'PROVENANCE.json')), metadata);
  });
});

test('wrong version, integrity, and mixed hash-size provenance are rejected', async () => {
  for (const mutate of [
    p => { p.version = '3.0.17'; },
    p => { p.npm_integrity = 'sha512-not-reviewed'; },
    p => { p.bundle.size_bytes = PATCHED_SIZE; },
    p => { p.upstream_bundle = { sha256: '0'.repeat(64) }; },
    p => { p.local_patches = [{ id: 'unreviewed-delta' }]; },
  ]) {
    await fixture(async (options, metadata) => {
      mutate(metadata);
      await writeFile(join(options.vendor, 'PROVENANCE.json'), JSON.stringify(metadata));
      await assert.rejects(applyVendorPatch(options), /provenance|proof/);
      assert.deepEqual(await readFile(join(options.vendor, 'webtorrent.min.js')), source);
    });
  }
});

test('source or destination symlinks are never followed', async () => {
  await fixture(async options => {
    const linked = options.source + '.link';
    await symlink(options.source, linked);
    await assert.rejects(applyVendorPatch({ ...options, source: linked }), /unsafe/);
    await rm(join(options.vendor, 'webtorrent.min.js'));
    await symlink(options.source, join(options.vendor, 'webtorrent.min.js'));
    await assert.rejects(applyVendorPatch(options), /unsafe/);
    assert.deepEqual(await readFile(options.source), source);
  });
});
