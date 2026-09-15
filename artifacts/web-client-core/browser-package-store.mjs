const SHA256 = /^[0-9a-f]{64}$/;
const MAX_PACKAGE_BYTES = 64 * 1024 * 1024;
const MAX_PACKAGE_FILES = 128;
const MIN_CACHE_BYTES = MAX_PACKAGE_BYTES;
const MAX_CACHE_BYTES = 2 * 1024 * 1024 * 1024;
const DEFAULT_CACHE_BYTES = 256 * 1024 * 1024;
const DEFAULT_STAGING_TTL_MS = 10 * 60 * 1000;
const SAFE_PACKAGE_PATH = /^(?!\/)(?!.*(?:^|\/)\.{1,2}(?:\/|$))(?!.*\/\/)[A-Za-z0-9._/-]{1,240}$/;

function fail(reason) {
  throw new Error('vibapp-browser-package-store:' + reason);
}

function record(value, reason) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail(reason);
  return value;
}

function boundedInteger(value, fallback, minimum, maximum, reason) {
  const selected = value === undefined ? fallback : value;
  if (!Number.isSafeInteger(selected) || selected < minimum || selected > maximum) fail(reason);
  return selected;
}

function exactBytes(value) {
  if (value instanceof ArrayBuffer) return new Uint8Array(value);
  if (ArrayBuffer.isView(value)) return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  fail('file-bytes-type');
}

function safeRelativePath(value, reason = 'file-path') {
  if (
    typeof value !== 'string'
    || !SAFE_PACKAGE_PATH.test(value)
  ) fail(reason);
  return value;
}

function descriptor(value, index) {
  record(value, 'file-' + index);
  const path = safeRelativePath(value.path);
  if (!SHA256.test(value.sha256 || '')) fail('file-sha256');
  const sizeBytes = boundedInteger(value.sizeBytes, undefined, 1, MAX_PACKAGE_BYTES, 'file-size');
  return Object.freeze({ path, sha256: value.sha256, sizeBytes });
}

function packagePlan(input) {
  record(input, 'package-plan');
  if (!SHA256.test(input.packageDigestSha256 || '')) fail('package-digest');
  if (!Array.isArray(input.files) || input.files.length < 2 || input.files.length > MAX_PACKAGE_FILES) {
    fail('file-count');
  }
  const paths = new Set();
  let sizeBytes = 0;
  const files = input.files.map((item, index) => {
    const selected = descriptor(item, index);
    if (paths.has(selected.path)) fail('file-duplicate');
    paths.add(selected.path);
    sizeBytes += selected.sizeBytes;
    if (!Number.isSafeInteger(sizeBytes) || sizeBytes > MAX_PACKAGE_BYTES) fail('package-size');
    return selected;
  });
  if (!paths.has('candidate.json') || !paths.has('package/manifest.json')) fail('package-layout');
  if (sizeBytes < 2) fail('package-size');
  if (input.sizeBytes !== undefined && input.sizeBytes !== sizeBytes) fail('package-size');
  return Object.freeze({
    packageDigestSha256: input.packageDigestSha256,
    files: Object.freeze(files),
    fileCount: files.length,
    sizeBytes,
  });
}

function sameInventory(left, right) {
  if (!left || left.sizeBytes !== right.sizeBytes || left.fileCount !== right.fileCount) return false;
  const observed = new Map((left.files || []).map(item => [item.path, item]));
  return right.files.every(item => {
    const found = observed.get(item.path);
    return found?.sha256 === item.sha256 && found?.sizeBytes === item.sizeBytes;
  });
}

async function sha256Hex(bytes) {
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
}

function publicReceipt(value) {
  return Object.freeze({
    schemaVersion: 'vibapp.browser-package-cache-receipt.experimental-v1',
    packageDigestSha256: value.packageDigestSha256,
    state: 'committed',
    fileCount: value.fileCount,
    sizeBytes: value.sizeBytes,
    committedAtUnixMs: value.committedAtUnixMs,
  });
}

function backendShape(value) {
  record(value, 'backend');
  for (const method of [
    'cleanupStaging',
    'getPackage',
    'replaceStaging',
    'putStagedFile',
    'commitStaging',
    'discardStaging',
    'readCommittedFile',
    'removeCommitted',
    'close',
  ]) {
    if (typeof value[method] !== 'function') fail('backend-' + method);
  }
  return value;
}

function randomStageToken() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
}

function transactionError(transaction, fallback) {
  return transaction.error || new Error('vibapp-browser-package-store:' + fallback);
}

function createIndexedDbBackend({ indexedDBFactory, databaseName }) {
  if (!indexedDBFactory || typeof indexedDBFactory.open !== 'function') fail('indexeddb-unavailable');
  if (typeof databaseName !== 'string' || !/^[a-zA-Z0-9._-]{1,96}$/.test(databaseName)) fail('database-name');
  let databasePromise;

  function database() {
    if (databasePromise) return databasePromise;
    databasePromise = new Promise((resolve, reject) => {
      const request = indexedDBFactory.open(databaseName, 1);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains('packages')) {
          db.createObjectStore('packages', { keyPath: 'packageDigestSha256' });
        }
        if (!db.objectStoreNames.contains('files')) {
          const files = db.createObjectStore('files', { keyPath: ['packageDigestSha256', 'path'] });
          files.createIndex('byPackage', 'packageDigestSha256', { unique: false });
        }
      };
      request.onerror = () => reject(request.error || new Error('indexeddb-open'));
      request.onblocked = () => reject(new Error('indexeddb-blocked'));
      request.onsuccess = () => resolve(request.result);
    });
    return databasePromise;
  }

  function deletePackageFiles(fileStore, packageDigestSha256, done, failTransaction) {
    const request = fileStore.index('byPackage').openCursor(packageDigestSha256);
    request.onerror = () => failTransaction(request.error || new Error('indexeddb-cursor'));
    request.onsuccess = () => {
      const cursor = request.result;
      if (!cursor) {
        done();
        return;
      }
      cursor.delete();
      cursor.continue();
    };
  }

  async function cleanupStaging(cutoffUnixMs) {
    const db = await database();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(['packages', 'files'], 'readwrite');
      const packages = transaction.objectStore('packages');
      const files = transaction.objectStore('files');
      let cause;
      const stop = error => {
        cause = error;
        try { transaction.abort(); } catch {}
      };
      const request = packages.getAll();
      request.onerror = () => stop(request.error || new Error('indexeddb-read'));
      request.onsuccess = () => {
        for (const item of request.result) {
          if (item.state !== 'staging' || item.createdAtUnixMs > cutoffUnixMs) continue;
          packages.delete(item.packageDigestSha256);
          deletePackageFiles(files, item.packageDigestSha256, () => {}, stop);
        }
      };
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(cause || transactionError(transaction, 'cleanup'));
      transaction.onabort = () => reject(cause || transactionError(transaction, 'cleanup'));
    });
  }

  async function getPackage(packageDigestSha256) {
    const db = await database();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction('packages', 'readonly');
      const request = transaction.objectStore('packages').get(packageDigestSha256);
      request.onerror = () => reject(request.error || new Error('indexeddb-read'));
      request.onsuccess = () => resolve(request.result || null);
    });
  }

  async function replaceStaging(value) {
    const db = await database();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(['packages', 'files'], 'readwrite');
      const packages = transaction.objectStore('packages');
      const files = transaction.objectStore('files');
      let cause;
      const stop = error => {
        cause = error;
        try { transaction.abort(); } catch {}
      };
      const request = packages.get(value.packageDigestSha256);
      request.onerror = () => stop(request.error || new Error('indexeddb-read'));
      request.onsuccess = () => {
        if (request.result?.state === 'committed') {
          stop(new Error('vibapp-browser-package-store:already-committed'));
          return;
        }
        deletePackageFiles(files, value.packageDigestSha256, () => packages.put(value), stop);
      };
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(cause || transactionError(transaction, 'replace-staging'));
      transaction.onabort = () => reject(cause || transactionError(transaction, 'replace-staging'));
    });
  }

  async function putStagedFile(packageDigestSha256, stageToken, value) {
    const db = await database();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(['packages', 'files'], 'readwrite');
      const packages = transaction.objectStore('packages');
      const files = transaction.objectStore('files');
      let cause;
      const stop = error => {
        cause = error;
        try { transaction.abort(); } catch {}
      };
      const request = packages.get(packageDigestSha256);
      request.onerror = () => stop(request.error || new Error('indexeddb-read'));
      request.onsuccess = () => {
        const item = request.result;
        if (item?.state !== 'staging' || item.stageToken !== stageToken) {
          stop(new Error('vibapp-browser-package-store:stale-stage'));
          return;
        }
        const expected = item.files?.find(file => file.path === value.path);
        const byteLength = value.bytes instanceof ArrayBuffer ? value.bytes.byteLength : -1;
        if (
          !expected
          || expected.sha256 !== value.sha256
          || expected.sizeBytes !== value.sizeBytes
          || byteLength !== expected.sizeBytes
        ) {
          stop(new Error('vibapp-browser-package-store:file-descriptor-mismatch'));
          return;
        }
        files.put({ packageDigestSha256, ...value });
      };
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(cause || transactionError(transaction, 'write-file'));
      transaction.onabort = () => reject(cause || transactionError(transaction, 'write-file'));
    });
  }

  async function commitStaging(packageDigestSha256, stageToken, cacheLimitBytes, committedAtUnixMs) {
    const db = await database();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(['packages', 'files'], 'readwrite');
      const packages = transaction.objectStore('packages');
      const files = transaction.objectStore('files');
      let cause;
      let packageValue;
      let storedFileKeys;
      let allPackages;
      let pending = 3;
      const stop = error => {
        cause = error;
        try { transaction.abort(); } catch {}
      };
      const finish = () => {
        pending -= 1;
        if (pending !== 0) return;
        if (packageValue?.state !== 'staging' || packageValue.stageToken !== stageToken) {
          stop(new Error('vibapp-browser-package-store:stale-stage'));
          return;
        }
        const storedPaths = new Set(storedFileKeys.map(key => Array.isArray(key) ? key[1] : null));
        if (
          storedPaths.size !== packageValue.fileCount
          || packageValue.files.some(file => !storedPaths.has(file.path))
        ) {
          stop(new Error('vibapp-browser-package-store:incomplete-stage'));
          return;
        }
        const committedBytes = allPackages
          .filter(item => item.state === 'committed' && item.packageDigestSha256 !== packageDigestSha256)
          .reduce((sum, item) => sum + item.sizeBytes, 0);
        if (!Number.isSafeInteger(committedBytes) || committedBytes + packageValue.sizeBytes > cacheLimitBytes) {
          stop(new Error('vibapp-browser-package-store:cache-limit'));
          return;
        }
        const committed = {
          ...packageValue,
          state: 'committed',
          stageToken: null,
          committedAtUnixMs,
        };
        packages.put(committed);
        packageValue = committed;
      };
      const packageRequest = packages.get(packageDigestSha256);
      packageRequest.onerror = () => stop(packageRequest.error || new Error('indexeddb-read'));
      packageRequest.onsuccess = () => { packageValue = packageRequest.result; finish(); };
      const filesRequest = files.index('byPackage').getAllKeys(packageDigestSha256);
      filesRequest.onerror = () => stop(filesRequest.error || new Error('indexeddb-read'));
      filesRequest.onsuccess = () => { storedFileKeys = filesRequest.result; finish(); };
      const packagesRequest = packages.getAll();
      packagesRequest.onerror = () => stop(packagesRequest.error || new Error('indexeddb-read'));
      packagesRequest.onsuccess = () => { allPackages = packagesRequest.result; finish(); };
      transaction.oncomplete = () => resolve(packageValue);
      transaction.onerror = () => reject(cause || transactionError(transaction, 'commit'));
      transaction.onabort = () => reject(cause || transactionError(transaction, 'commit'));
    });
  }

  async function discardStaging(packageDigestSha256, stageToken) {
    const db = await database();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(['packages', 'files'], 'readwrite');
      const packages = transaction.objectStore('packages');
      const files = transaction.objectStore('files');
      let cause;
      const stop = error => {
        cause = error;
        try { transaction.abort(); } catch {}
      };
      const request = packages.get(packageDigestSha256);
      request.onerror = () => stop(request.error || new Error('indexeddb-read'));
      request.onsuccess = () => {
        const item = request.result;
        if (item?.state !== 'staging' || item.stageToken !== stageToken) return;
        deletePackageFiles(files, packageDigestSha256, () => packages.delete(packageDigestSha256), stop);
      };
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(cause || transactionError(transaction, 'discard'));
      transaction.onabort = () => reject(cause || transactionError(transaction, 'discard'));
    });
  }

  async function readCommittedFile(packageDigestSha256, path) {
    const db = await database();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(['packages', 'files'], 'readonly');
      const packages = transaction.objectStore('packages');
      const files = transaction.objectStore('files');
      let packageValue;
      let fileValue;
      let pending = 2;
      const finish = () => {
        pending -= 1;
        if (pending !== 0) return;
        if (packageValue?.state !== 'committed' || !fileValue) {
          reject(new Error('vibapp-browser-package-store:not-found'));
          return;
        }
        resolve(fileValue.bytes);
      };
      const packageRequest = packages.get(packageDigestSha256);
      packageRequest.onerror = () => reject(packageRequest.error || new Error('indexeddb-read'));
      packageRequest.onsuccess = () => { packageValue = packageRequest.result; finish(); };
      const fileRequest = files.get([packageDigestSha256, path]);
      fileRequest.onerror = () => reject(fileRequest.error || new Error('indexeddb-read'));
      fileRequest.onsuccess = () => { fileValue = fileRequest.result; finish(); };
    });
  }

  async function removeCommitted(packageDigestSha256) {
    const db = await database();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(['packages', 'files'], 'readwrite');
      const packages = transaction.objectStore('packages');
      const files = transaction.objectStore('files');
      let cause;
      const stop = error => {
        cause = error;
        try { transaction.abort(); } catch {}
      };
      const request = packages.get(packageDigestSha256);
      request.onerror = () => stop(request.error || new Error('indexeddb-read'));
      request.onsuccess = () => {
        if (request.result?.state !== 'committed') return;
        deletePackageFiles(files, packageDigestSha256, () => packages.delete(packageDigestSha256), stop);
      };
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(cause || transactionError(transaction, 'remove'));
      transaction.onabort = () => reject(cause || transactionError(transaction, 'remove'));
    });
  }

  async function close() {
    if (!databasePromise) return;
    const db = await databasePromise;
    db.close();
    databasePromise = null;
  }

  return Object.freeze({
    cleanupStaging,
    getPackage,
    replaceStaging,
    putStagedFile,
    commitStaging,
    discardStaging,
    readCommittedFile,
    removeCommitted,
    close,
  });
}

export function createBrowserPackageStore({
  backend,
  indexedDBFactory = globalThis.indexedDB,
  databaseName = 'vibapp-public-packages-v1',
  cacheLimitBytes = DEFAULT_CACHE_BYTES,
  stagingTtlMs = DEFAULT_STAGING_TTL_MS,
  now = () => Date.now(),
} = {}) {
  const selectedCacheLimit = boundedInteger(
    cacheLimitBytes,
    DEFAULT_CACHE_BYTES,
    MIN_CACHE_BYTES,
    MAX_CACHE_BYTES,
    'cache-limit',
  );
  const selectedStagingTtl = boundedInteger(stagingTtlMs, DEFAULT_STAGING_TTL_MS, 1_000, 24 * 60 * 60 * 1000, 'staging-ttl');
  if (typeof now !== 'function') fail('clock');
  const selectedBackend = backendShape(backend || createIndexedDbBackend({ indexedDBFactory, databaseName }));
  const activeStages = new Map();
  let cleanupPromise;

  async function cleanup() {
    if (!cleanupPromise) {
      cleanupPromise = selectedBackend.cleanupStaging(now() - selectedStagingTtl).finally(() => { cleanupPromise = null; });
    }
    return cleanupPromise;
  }

  async function begin(input) {
    const plan = packagePlan(input);
    if (activeStages.has(plan.packageDigestSha256)) fail('stage-active');
    await cleanup();
    const existing = await selectedBackend.getPackage(plan.packageDigestSha256);
    if (existing?.state === 'committed') {
      if (!sameInventory(existing, plan)) fail('committed-inventory-conflict');
      const receipt = publicReceipt(existing);
      return Object.freeze({
        alreadyCommitted: true,
        async writeFile() { fail('already-committed'); },
        async commit() { return receipt; },
        async abort() {},
      });
    }

    const stageToken = randomStageToken();
    const createdAtUnixMs = now();
    const storedPlan = {
      ...plan,
      files: plan.files.map(item => ({ ...item })),
      state: 'staging',
      stageToken,
      createdAtUnixMs,
      committedAtUnixMs: null,
    };
    await selectedBackend.replaceStaging(storedPlan);
    const observed = new Set();
    let terminal = false;
    const marker = Object.freeze({ stageToken });
    activeStages.set(plan.packageDigestSha256, marker);

    async function abort() {
      if (terminal) return;
      terminal = true;
      if (activeStages.get(plan.packageDigestSha256) === marker) activeStages.delete(plan.packageDigestSha256);
      await selectedBackend.discardStaging(plan.packageDigestSha256, stageToken);
    }

    async function writeFile(inputFile) {
      try {
        if (terminal) fail('stage-closed');
        record(inputFile, 'staged-file');
        const path = safeRelativePath(inputFile.path);
        if (observed.has(path)) fail('file-duplicate');
        const expected = plan.files.find(item => item.path === path);
        if (!expected || inputFile.sha256 !== expected.sha256 || inputFile.sizeBytes !== expected.sizeBytes) {
          fail('file-descriptor-mismatch');
        }
        const bytes = exactBytes(inputFile.bytes);
        if (bytes.byteLength !== expected.sizeBytes) fail('file-size-mismatch');
        if (await sha256Hex(bytes) !== expected.sha256) fail('file-integrity');
        await selectedBackend.putStagedFile(plan.packageDigestSha256, stageToken, {
          ...expected,
          bytes: bytes.byteOffset === 0 && bytes.byteLength === bytes.buffer.byteLength
            ? bytes.buffer
            : bytes.slice().buffer,
        });
        observed.add(path);
      } catch (error) {
        try { await abort(); } catch {}
        throw error;
      }
    }

    async function commit() {
      if (terminal) fail('stage-closed');
      if (observed.size !== plan.fileCount || plan.files.some(item => !observed.has(item.path))) {
        await abort();
        fail('incomplete-stage');
      }
      try {
        const committed = await selectedBackend.commitStaging(
          plan.packageDigestSha256,
          stageToken,
          selectedCacheLimit,
          now(),
        );
        terminal = true;
        if (activeStages.get(plan.packageDigestSha256) === marker) activeStages.delete(plan.packageDigestSha256);
        return publicReceipt(committed);
      } catch (error) {
        await abort();
        throw error;
      }
    }

    return Object.freeze({ alreadyCommitted: false, writeFile, commit, abort });
  }

  async function getReceipt(packageDigestSha256) {
    if (!SHA256.test(packageDigestSha256 || '')) fail('package-digest');
    const value = await selectedBackend.getPackage(packageDigestSha256);
    return value?.state === 'committed' ? publicReceipt(value) : null;
  }

  async function readFile(packageDigestSha256, path) {
    if (!SHA256.test(packageDigestSha256 || '')) fail('package-digest');
    const selectedPath = safeRelativePath(path);
    const value = await selectedBackend.getPackage(packageDigestSha256);
    if (value?.state !== 'committed') fail('not-found');
    const expected = value.files?.find(item => item.path === selectedPath);
    if (!expected) fail('not-found');
    const bytes = exactBytes(await selectedBackend.readCommittedFile(packageDigestSha256, selectedPath));
    if (bytes.byteLength !== expected.sizeBytes || await sha256Hex(bytes) !== expected.sha256) fail('committed-integrity');
    return bytes;
  }

  async function remove(packageDigestSha256) {
    if (!SHA256.test(packageDigestSha256 || '')) fail('package-digest');
    if (activeStages.has(packageDigestSha256)) fail('stage-active');
    await selectedBackend.removeCommitted(packageDigestSha256);
  }

  async function close() {
    const stages = [...activeStages.keys()];
    for (const digest of stages) {
      const value = await selectedBackend.getPackage(digest);
      if (value?.state === 'staging') await selectedBackend.discardStaging(digest, value.stageToken);
      activeStages.delete(digest);
    }
    await selectedBackend.close();
  }

  return Object.freeze({ begin, getReceipt, readFile, remove, close });
}
