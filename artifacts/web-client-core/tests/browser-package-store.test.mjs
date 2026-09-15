import assert from 'node:assert/strict';
import { createHash, webcrypto } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { createBrowserPackageStore } from '../browser-package-store.mjs';

if (!globalThis.crypto) globalThis.crypto = webcrypto;

const digest = bytes => createHash('sha256').update(bytes).digest('hex');
const encode = value => new TextEncoder().encode(value);

function plan(packageDigestSha256 = 'a'.repeat(64)) {
  const candidate = encode('{"candidate":true}');
  const manifest = encode('{"manifest":true}');
  return {
    packageDigestSha256,
    files: [
      { path: 'candidate.json', sha256: digest(candidate), sizeBytes: candidate.byteLength, bytes: candidate },
      { path: 'package/manifest.json', sha256: digest(manifest), sizeBytes: manifest.byteLength, bytes: manifest },
    ],
  };
}

function clone(value) {
  return value === null || value === undefined ? value : structuredClone(value);
}

function memoryBackend({ committedBytes = 0, failCommit = false } = {}) {
  const packages = new Map();
  const files = new Map();
  const calls = { discard: 0, commit: 0, close: 0 };
  if (committedBytes > 0) {
    packages.set('f'.repeat(64), {
      packageDigestSha256: 'f'.repeat(64),
      state: 'committed',
      stageToken: null,
      files: [],
      fileCount: 0,
      sizeBytes: committedBytes,
      committedAtUnixMs: 1,
    });
  }
  const packageFiles = packageDigestSha256 => {
    if (!files.has(packageDigestSha256)) files.set(packageDigestSha256, new Map());
    return files.get(packageDigestSha256);
  };
  const backend = {
    async cleanupStaging(cutoffUnixMs) {
      for (const [key, value] of packages) {
        if (value.state === 'staging' && value.createdAtUnixMs <= cutoffUnixMs) {
          packages.delete(key);
          files.delete(key);
        }
      }
    },
    async getPackage(packageDigestSha256) {
      return clone(packages.get(packageDigestSha256) || null);
    },
    async replaceStaging(value) {
      if (packages.get(value.packageDigestSha256)?.state === 'committed') {
        throw new Error('already-committed');
      }
      packages.set(value.packageDigestSha256, clone(value));
      files.set(value.packageDigestSha256, new Map());
    },
    async putStagedFile(packageDigestSha256, stageToken, value) {
      const owner = packages.get(packageDigestSha256);
      if (owner?.state !== 'staging' || owner.stageToken !== stageToken) throw new Error('stale-stage');
      packageFiles(packageDigestSha256).set(value.path, clone(value));
    },
    async commitStaging(packageDigestSha256, stageToken, cacheLimitBytes, committedAtUnixMs) {
      calls.commit += 1;
      const owner = packages.get(packageDigestSha256);
      const stagedFiles = [...(files.get(packageDigestSha256)?.values() || [])];
      if (owner?.state !== 'staging' || owner.stageToken !== stageToken) throw new Error('stale-stage');
      if (failCommit) throw new Error('injected-commit-failure');
      const committedTotal = [...packages.values()]
        .filter(item => item.state === 'committed')
        .reduce((sum, item) => sum + item.sizeBytes, 0);
      if (committedTotal + owner.sizeBytes > cacheLimitBytes) {
        throw new Error('vibapp-browser-package-store:cache-limit');
      }
      assert.equal(stagedFiles.length, owner.fileCount);
      const committed = { ...owner, state: 'committed', stageToken: null, committedAtUnixMs };
      packages.set(packageDigestSha256, committed);
      return clone(committed);
    },
    async discardStaging(packageDigestSha256, stageToken) {
      calls.discard += 1;
      const owner = packages.get(packageDigestSha256);
      if (owner?.state === 'staging' && owner.stageToken === stageToken) {
        packages.delete(packageDigestSha256);
        files.delete(packageDigestSha256);
      }
    },
    async readCommittedFile(packageDigestSha256, path) {
      if (packages.get(packageDigestSha256)?.state !== 'committed') throw new Error('not-found');
      return clone(files.get(packageDigestSha256)?.get(path)?.bytes);
    },
    async removeCommitted(packageDigestSha256) {
      if (packages.get(packageDigestSha256)?.state === 'committed') {
        packages.delete(packageDigestSha256);
        files.delete(packageDigestSha256);
      }
    },
    async close() { calls.close += 1; },
  };
  return { backend, packages, files, calls };
}

test('stages bounded verified files and atomically exposes only a small committed receipt', async () => {
  const memory = memoryBackend();
  const store = createBrowserPackageStore({ backend: memory.backend, now: () => 1234 });
  const fixture = plan();
  const stage = await store.begin(fixture);
  assert.equal(stage.alreadyCommitted, false);
  for (const file of fixture.files) await stage.writeFile(file);
  const receipt = await stage.commit();
  assert.deepEqual(receipt, {
    schemaVersion: 'vibapp.browser-package-cache-receipt.experimental-v1',
    packageDigestSha256: fixture.packageDigestSha256,
    state: 'committed',
    fileCount: 2,
    sizeBytes: fixture.files.reduce((sum, file) => sum + file.sizeBytes, 0),
    committedAtUnixMs: 1234,
  });
  assert.doesNotMatch(JSON.stringify(receipt), /magnet|blob:|data:|"bytes"|"files"/i);
  assert.deepEqual(await store.readFile(fixture.packageDigestSha256, 'candidate.json'), fixture.files[0].bytes);
  assert.deepEqual(await store.getReceipt(fixture.packageDigestSha256), receipt);
  await store.remove(fixture.packageDigestSha256);
  assert.equal(await store.getReceipt(fixture.packageDigestSha256), null);
  await store.close();
  assert.equal(memory.calls.close, 1);
});

test('file-integrity failure deletes the whole hidden staging package', async () => {
  const memory = memoryBackend();
  const store = createBrowserPackageStore({ backend: memory.backend });
  const fixture = plan();
  const stage = await store.begin(fixture);
  await assert.rejects(stage.writeFile({ ...fixture.files[0], bytes: encode('tampered') }), /file-size-mismatch|file-integrity/);
  assert.equal(memory.packages.has(fixture.packageDigestSha256), false);
  assert.equal(memory.files.has(fixture.packageDigestSha256), false);
  assert.equal(memory.calls.discard, 1);
});

test('incomplete and backend-failed commits leave no readable partial package', async () => {
  const incompleteMemory = memoryBackend();
  const incompleteStore = createBrowserPackageStore({ backend: incompleteMemory.backend });
  const fixture = plan();
  const incomplete = await incompleteStore.begin(fixture);
  await incomplete.writeFile(fixture.files[0]);
  await assert.rejects(incomplete.commit(), /incomplete-stage/);
  assert.equal(await incompleteStore.getReceipt(fixture.packageDigestSha256), null);

  const failedMemory = memoryBackend({ failCommit: true });
  const failedStore = createBrowserPackageStore({ backend: failedMemory.backend });
  const failed = await failedStore.begin(fixture);
  for (const file of fixture.files) await failed.writeFile(file);
  await assert.rejects(failed.commit(), /injected-commit-failure/);
  assert.equal(await failedStore.getReceipt(fixture.packageDigestSha256), null);
  assert.equal(failedMemory.packages.has(fixture.packageDigestSha256), false);
});

test('cache limit is enforced before the staging-to-committed state flip', async () => {
  const memory = memoryBackend({ committedBytes: 64 * 1024 * 1024 });
  const store = createBrowserPackageStore({ backend: memory.backend, cacheLimitBytes: 64 * 1024 * 1024 });
  const fixture = plan();
  const stage = await store.begin(fixture);
  for (const file of fixture.files) await stage.writeFile(file);
  await assert.rejects(stage.commit(), /cache-limit/);
  assert.equal(memory.packages.has(fixture.packageDigestSha256), false);
  assert.equal(await store.getReceipt(fixture.packageDigestSha256), null);
});

test('same committed digest is idempotent only for the exact inventory', async () => {
  const memory = memoryBackend();
  const store = createBrowserPackageStore({ backend: memory.backend });
  const fixture = plan();
  const first = await store.begin(fixture);
  for (const file of fixture.files) await first.writeFile(file);
  const receipt = await first.commit();
  const repeated = await store.begin(fixture);
  assert.equal(repeated.alreadyCommitted, true);
  assert.deepEqual(await repeated.commit(), receipt);
  const changed = { ...fixture, files: fixture.files.map((file, index) => index ? file : { ...file, sha256: '0'.repeat(64) }) };
  await assert.rejects(store.begin(changed), /committed-inventory-conflict/);
});

test('bounds reject traversal, duplicate paths, too many files, and invalid cache sizes', async () => {
  const memory = memoryBackend();
  const fixture = plan();
  const store = createBrowserPackageStore({ backend: memory.backend });
  await assert.rejects(store.begin({ ...fixture, files: [fixture.files[0], { ...fixture.files[1], path: '../manifest.json' }] }), /file-path/);
  await assert.rejects(store.begin({ ...fixture, files: [fixture.files[0], { ...fixture.files[1], path: fixture.files[0].path }] }), /file-duplicate/);
  await assert.rejects(store.begin({ ...fixture, files: Array.from({ length: 129 }, (_, index) => ({
    path: index === 0 ? 'candidate.json' : index === 1 ? 'package/manifest.json' : `package/${index}.bin`,
    sha256: '0'.repeat(64),
    sizeBytes: 0,
  })) }), /file-count/);
  assert.throws(() => createBrowserPackageStore({ backend: memory.backend, cacheLimitBytes: 1024 }), /cache-limit/);
});

test('production module uses IndexedDB records and never creates executable URLs', async () => {
  const source = await readFile(new URL('../browser-package-store.mjs', import.meta.url), 'utf8');
  assert.match(source, /indexedDBFactory\.open/);
  assert.match(source, /files\.index\('byPackage'\)\.getAllKeys/);
  assert.doesNotMatch(source, /files\.index\('byPackage'\)\.getAll\(/);
  assert.doesNotMatch(source, /createObjectURL|blob:|data:text|data:application|document\.|window\.|navigator\./);
});
