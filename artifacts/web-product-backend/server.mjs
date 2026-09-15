#!/usr/bin/env node

import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { createHash, timingSafeEqual } from 'node:crypto';
import { constants as fsConstants } from 'node:fs';
import { chmod, lstat, mkdir, mkdtemp, open, realpath, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { isAbsolute, join, resolve } from 'node:path';

const REQUEST_VERSION = 'vibapp.web-product-request.experimental-v1';
const RESPONSE_VERSION = 'vibapp.web-product-response.experimental-v1';
const MAX_REQUEST_BYTES = 512 * 1024;
const MAX_RESPONSE_BYTES = 2 * 1024 * 1024;
const MAX_STDERR_BYTES = 64 * 1024;
const REQUEST_TIMEOUT_MS = 75_000;
const MAX_CONCURRENCY = 4;
const MAX_BRIDGE_EXECUTABLE_BYTES = 512 * 1024 * 1024;
const CHILD_ERROR_FORCE_KILL_MS = 5_000;
const CHILD_TERMINATION_GRACE_MS = 1_000;
const CONFIG_DISPOSE_GRACE_MS = 1_500;
const SHA256 = /^[0-9a-f]{64}$/;
const OWNED_BRIDGE_SNAPSHOTS = new WeakSet();
const BRIDGE_SNAPSHOT_HANDLES = new WeakMap();
const EXPECTED_BRIDGE_CONTRACTS = Object.freeze({
  cloud_codeagent_task: 'vibapp.cloud-codeagent-task.experimental-v3',
  cloud_codeagent_task_schema_sha256: 'fb2a49186349aa4542ab6bab9440fe2e5c48a9d7cc86085449a71e20ecdb73ab',
  codeagent_adapter: 'vibapp.codeagent-adapter.experimental-v1',
  codeagent_status: 'vibapp.codeagent-adapter-status.experimental-v2',
  codex_compatibility_policy: 'vibapp.codex-cli-compatibility.experimental-v1',
  delivery_worker: 'vibapp.desktop-delivery-worker.experimental-v2',
  provider_execution_identity: 'vibapp.provider-execution-identity.experimental-v1',
  source_handoff: 'vibapp.codeagent-source-handoff.experimental-v2',
});
const COMMANDS = new Set([
  'health',
  'get_state',
  'submit_need',
  'complete_need',
  'get_model_settings',
  'save_model_settings',
  'get_codeagent_settings',
  'save_codeagent_settings',
  'get_network_settings',
  'save_network_settings',
  'submit_development_task',
]);

function exactKeys(value, expected) {
  return value && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).sort().join(',') === expected;
}

export function validateBridgeHealth(response, expectedBuildInputsSha256) {
  if (!SHA256.test(expectedBuildInputsSha256 || '')) {
    throw new Error('VIBAPP_EXPECTED_DESKTOP_BUILD_INPUTS_SHA256 must be a lowercase SHA-256');
  }
  if (
    !exactKeys(response, 'error,ok,result,schema_version')
    || response.schema_version !== 'vibapp.product-bridge-response.experimental-v1'
    || response.ok !== true
    || response.error !== null
    || !exactKeys(
      response.result,
      'bridge_lifetime_margin_seconds,bridge_lifetime_seconds,build_input_receipt,contracts,lifecycle,pipeline_budget_seconds,schema_version,service,status,worker_cleanup_budget_seconds',
    )
    || response.result.schema_version !== 'vibapp.product-bridge.experimental-v1'
    || response.result.service !== 'vibapp-product-bridge'
    || response.result.status !== 'ok'
    || response.result.lifecycle !== 'cancel-join-confirm'
    || response.result.pipeline_budget_seconds !== 2700
    || response.result.bridge_lifetime_seconds !== 2760
    || response.result.bridge_lifetime_margin_seconds !== 60
    || response.result.worker_cleanup_budget_seconds !== 20
    || !exactKeys(response.result.build_input_receipt, 'schema_version,sha256')
    || response.result.build_input_receipt.schema_version !== 'vibapp.desktop-build-input-receipt.experimental-v1'
    || response.result.build_input_receipt.sha256 !== expectedBuildInputsSha256
    || !exactKeys(response.result.contracts, Object.keys(EXPECTED_BRIDGE_CONTRACTS).sort().join(','))
    || Object.entries(EXPECTED_BRIDGE_CONTRACTS).some(([name, value]) => response.result.contracts[name] !== value)
  ) throw new Error('product-bridge-health-mismatch');
  return response.result;
}

export function boundedEnvironment(source = process.env) {
  const environment = {
    HOME: source.HOME || '',
    PATH: source.PATH || '/usr/local/bin:/usr/bin:/bin',
    LANG: 'C.UTF-8',
    LC_ALL: 'C.UTF-8',
    TZ: 'UTC',
  };
  for (const name of [
    'VIBAPP_BUILDER_TOOL_LAYER',
    'VIBAPP_BUILDER_CARGO_HOME',
    'VIBAPP_BUILDER_CACHE_ACCEPTANCE',
    'VIBAPP_VERIFIER_WASM_TOOLS',
  ]) {
    if (source[name]) environment[name] = source[name];
  }
  if (source.VIBAPP_PUBLIC_REGISTRY_ROOT) {
    if (
      !isAbsolute(source.VIBAPP_PUBLIC_REGISTRY_ROOT)
      || source.VIBAPP_PUBLIC_REGISTRY_ROOT.length > 4096
      || /[\0\r\n]/.test(source.VIBAPP_PUBLIC_REGISTRY_ROOT)
    ) throw new Error('VIBAPP_PUBLIC_REGISTRY_ROOT must be a bounded absolute path');
    environment.VIBAPP_PUBLIC_REGISTRY_ROOT = source.VIBAPP_PUBLIC_REGISTRY_ROOT;
  }
  return environment;
}

function sameFileVersion(left, right) {
  return left.dev === right.dev
    && left.ino === right.ino
    && left.mode === right.mode
    && left.size === right.size
    && left.mtimeNs === right.mtimeNs
    && left.ctimeNs === right.ctimeNs;
}

async function digestFileHandle(handle, sizeBytes) {
  const hash = createHash('sha256');
  const buffer = Buffer.allocUnsafe(64 * 1024);
  let position = 0;
  while (position < sizeBytes) {
    const length = Math.min(buffer.byteLength, sizeBytes - position);
    const { bytesRead } = await handle.read(buffer, 0, length, position);
    if (bytesRead === 0) throw new Error('product bridge executable changed while reading');
    hash.update(buffer.subarray(0, bytesRead));
    position += bytesRead;
  }
  const extra = Buffer.allocUnsafe(1);
  if ((await handle.read(extra, 0, 1, position)).bytesRead !== 0) {
    throw new Error('product bridge executable changed while reading');
  }
  return hash.digest('hex');
}

async function openOrdinaryExecutable(path) {
  if (!isAbsolute(path)) throw new Error('VIBAPP_PRODUCT_BRIDGE_BIN must be absolute');
  const namedMetadata = await lstat(path, { bigint: true });
  if (
    !namedMetadata.isFile()
    || namedMetadata.isSymbolicLink()
    || (namedMetadata.mode & 0o111n) === 0n
  ) throw new Error('VIBAPP_PRODUCT_BRIDGE_BIN must be an executable ordinary file');

  const canonicalPath = await realpath(path);
  const handle = await open(
    canonicalPath,
    fsConstants.O_RDONLY | (fsConstants.O_NOFOLLOW || 0),
  );
  try {
    const openedMetadata = await handle.stat({ bigint: true });
    if (
      !openedMetadata.isFile()
      || (openedMetadata.mode & 0o111n) === 0n
      || openedMetadata.dev !== namedMetadata.dev
      || openedMetadata.ino !== namedMetadata.ino
      || openedMetadata.size > BigInt(MAX_BRIDGE_EXECUTABLE_BYTES)
    ) throw new Error('VIBAPP_PRODUCT_BRIDGE_BIN changed or exceeds the executable limit');
    return { handle, metadata: openedMetadata };
  } catch (error) {
    await handle.close();
    throw error;
  }
}

async function writeAll(handle, buffer) {
  let offset = 0;
  while (offset < buffer.byteLength) {
    const { bytesWritten } = await handle.write(
      buffer,
      offset,
      buffer.byteLength - offset,
      null,
    );
    if (bytesWritten === 0) throw new Error('product bridge snapshot write made no progress');
    offset += bytesWritten;
  }
}

async function snapshotExecutable(path) {
  const source = await openOrdinaryExecutable(path);
  let directory = null;
  let snapshotHandle = null;
  try {
    directory = await mkdtemp(join(tmpdir(), 'vibapp-web-product-bridge-'));
    await chmod(directory, 0o700);
    const snapshotPath = join(directory, 'vibapp-product-bridge');
    const target = await open(
      snapshotPath,
      fsConstants.O_WRONLY | fsConstants.O_CREAT | fsConstants.O_EXCL,
      0o500,
    );
    const sizeBytes = Number(source.metadata.size);
    const hash = createHash('sha256');
    const buffer = Buffer.allocUnsafe(64 * 1024);
    let position = 0;
    try {
      while (position < sizeBytes) {
        const length = Math.min(buffer.byteLength, sizeBytes - position);
        const { bytesRead } = await source.handle.read(buffer, 0, length, position);
        if (bytesRead === 0) throw new Error('product bridge executable changed while snapshotting');
        const chunk = buffer.subarray(0, bytesRead);
        hash.update(chunk);
        await writeAll(target, chunk);
        position += bytesRead;
      }
      const extra = Buffer.allocUnsafe(1);
      if ((await source.handle.read(extra, 0, 1, position)).bytesRead !== 0) {
        throw new Error('product bridge executable changed while snapshotting');
      }
      await target.sync();
    } finally {
      await target.close();
    }

    const afterSource = await source.handle.stat({ bigint: true });
    if (!sameFileVersion(source.metadata, afterSource)) {
      throw new Error('product bridge executable changed while snapshotting');
    }
    await chmod(snapshotPath, 0o500);
    const snapshotMetadata = await lstat(snapshotPath, { bigint: true });
    const snapshotDigest = hash.digest('hex');
    if (
      !snapshotMetadata.isFile()
      || snapshotMetadata.isSymbolicLink()
      || (snapshotMetadata.mode & 0o111n) === 0n
      || snapshotMetadata.size !== source.metadata.size
    ) throw new Error('product bridge snapshot is not an executable ordinary file');

    snapshotHandle = await open(
      snapshotPath,
      fsConstants.O_RDONLY | (fsConstants.O_NOFOLLOW || 0),
    );
    const verifiedMetadata = await snapshotHandle.stat({ bigint: true });
    const verifiedDigest = await digestFileHandle(snapshotHandle, sizeBytes);
    const afterDigestMetadata = await snapshotHandle.stat({ bigint: true });
    if (
      !sameFileVersion(verifiedMetadata, afterDigestMetadata)
      || verifiedDigest !== snapshotDigest
    ) throw new Error('product bridge snapshot digest mismatch');

    const snapshot = Object.freeze({
      path: await realpath(snapshotPath),
      directory,
      sha256: snapshotDigest,
      sizeBytes,
      device: String(snapshotMetadata.dev),
      inode: String(snapshotMetadata.ino),
    });
    OWNED_BRIDGE_SNAPSHOTS.add(snapshot);
    BRIDGE_SNAPSHOT_HANDLES.set(snapshot, snapshotHandle);
    snapshotHandle = null;
    return snapshot;
  } catch (error) {
    if (snapshotHandle) await snapshotHandle.close();
    if (directory) await rm(directory, { recursive: true, force: true });
    throw error;
  } finally {
    await source.handle.close();
  }
}

async function verifyBridgeSnapshot(snapshot) {
  if (!snapshot || !OWNED_BRIDGE_SNAPSHOTS.has(snapshot) || !SHA256.test(snapshot.sha256 || '')) {
    throw new Error('product bridge executable snapshot is not bound');
  }
  const handle = BRIDGE_SNAPSHOT_HANDLES.get(snapshot);
  if (!handle) throw new Error('product bridge executable snapshot handle is unavailable');
  const named = await lstat(snapshot.path, { bigint: true });
  if (
    !named.isFile()
    || named.isSymbolicLink()
    || (named.mode & 0o111n) === 0n
    || String(named.dev) !== snapshot.device
    || String(named.ino) !== snapshot.inode
    || Number(named.size) !== snapshot.sizeBytes
  ) throw new Error('product bridge executable snapshot identity mismatch');
  const before = await handle.stat({ bigint: true });
  if (
    !before.isFile()
    || (before.mode & 0o111n) === 0n
    || String(before.dev) !== snapshot.device
    || String(before.ino) !== snapshot.inode
    || Number(before.size) !== snapshot.sizeBytes
  ) throw new Error('product bridge executable snapshot identity mismatch');
  const digest = await digestFileHandle(handle, snapshot.sizeBytes);
  const after = await handle.stat({ bigint: true });
  if (!sameFileVersion(before, after) || digest !== snapshot.sha256) {
    throw new Error('product bridge executable snapshot digest mismatch');
  }
  return { executable: snapshot.path, inheritedFd: null };
}

async function privateDataDirectory(path) {
  if (!isAbsolute(path)) throw new Error('VIBAPP_WEB_PRODUCT_DATA_DIR must be absolute');
  await mkdir(path, { recursive: true, mode: 0o700 });
  const metadata = await lstat(path);
  if (!metadata.isDirectory() || metadata.isSymbolicLink()) {
    throw new Error('VIBAPP_WEB_PRODUCT_DATA_DIR must be an ordinary directory');
  }
  await chmod(path, 0o700);
  return realpath(path);
}

function authorized(value, expected) {
  if (typeof value !== 'string' || !value.startsWith('Bearer ')) return false;
  const actual = Buffer.from(value.slice(7));
  const wanted = Buffer.from(expected);
  return actual.length === wanted.length && timingSafeEqual(actual, wanted);
}

function send(response, status, value) {
  let payload = Buffer.from(JSON.stringify(value));
  if (payload.byteLength > MAX_RESPONSE_BYTES) {
    status = 500;
    payload = Buffer.from(JSON.stringify({
      schema_version: RESPONSE_VERSION,
      ok: false,
      result: null,
      error: 'response-limit-exceeded',
    }));
  }
  response.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': payload.byteLength,
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
    Connection: 'close',
  });
  response.end(payload);
}

function readBody(request) {
  return new Promise((resolveBody, rejectBody) => {
    const declared = request.headers['content-length'];
    if (!declared || !/^\d+$/.test(declared) || Number(declared) > MAX_REQUEST_BYTES) {
      rejectBody(new Error('request-size-invalid'));
      return;
    }
    const chunks = [];
    let size = 0;
    request.on('data', chunk => {
      size += chunk.byteLength;
      if (size > MAX_REQUEST_BYTES) {
        rejectBody(new Error('request-limit-exceeded'));
        request.destroy();
        return;
      }
      chunks.push(chunk);
    });
    request.once('end', () => resolveBody(Buffer.concat(chunks)));
    request.once('error', rejectBody);
  });
}

function notifyChildrenDrained(config) {
  if (config.children.size !== 0) return;
  for (const resolveWaiter of config.childDrainWaiters) resolveWaiter(true);
  config.childDrainWaiters.clear();
}

function waitForChildrenDrained(config, timeoutMs) {
  if (config.children.size === 0) return Promise.resolve(true);
  return new Promise(resolveWait => {
    let timer;
    const complete = drained => {
      clearTimeout(timer);
      config.childDrainWaiters.delete(complete);
      resolveWait(drained);
    };
    config.childDrainWaiters.add(complete);
    timer = setTimeout(() => complete(false), timeoutMs);
  });
}

function signalChildren(config, signal) {
  for (const child of config.children) {
    try {
      child.kill(signal);
    } catch {
      // Retain the child and its admission slot until `close`; a failed signal
      // is never evidence that the OS process no longer exists.
    }
  }
}

export async function disposeProductConfig(config) {
  if (!config) return;
  if (config.disposePromise) return config.disposePromise;
  config.disposing = true;
  config.disposePromise = (async () => {
    signalChildren(config, 'SIGTERM');
    if (!await waitForChildrenDrained(config, CONFIG_DISPOSE_GRACE_MS)) {
      signalChildren(config, 'SIGKILL');
      if (!await waitForChildrenDrained(config, CONFIG_DISPOSE_GRACE_MS)) {
        throw new Error('product bridge child cleanup timeout');
      }
    }
    const snapshot = config.bridgeSnapshot;
    if (snapshot && OWNED_BRIDGE_SNAPSHOTS.has(snapshot)) {
      const handle = BRIDGE_SNAPSHOT_HANDLES.get(snapshot);
      if (handle) await handle.close();
      BRIDGE_SNAPSHOT_HANDLES.delete(snapshot);
      await rm(snapshot.directory, { recursive: true, force: true });
      OWNED_BRIDGE_SNAPSHOTS.delete(snapshot);
    }
    config.disposed = true;
  })();
  return config.disposePromise;
}

async function invokeBridge(config, command, args) {
  if (config.disposing || config.disposed) throw new Error('product-bridge-shutting-down');
  if (config.slots.size >= MAX_CONCURRENCY) throw new Error('product-bridge-busy');
  const slot = Symbol(command);
  config.slots.add(slot);
  let childStarted = false;
  try {
    const bridge = await verifyBridgeSnapshot(config.bridgeSnapshot);
    if (config.disposing || config.disposed) throw new Error('product-bridge-shutting-down');
    return await new Promise((resolveCall, rejectCall) => {
      const stdio = ['pipe', 'pipe', 'pipe'];
      if (bridge.inheritedFd !== null) stdio.push(bridge.inheritedFd);
      const child = spawn(bridge.executable, [config.dataDir], {
        env: boundedEnvironment(),
        stdio,
      });
      childStarted = true;
      config.children.add(child);
      let stdout = Buffer.alloc(0);
      let stderrBytes = 0;
      let settled = false;
      let lifecycleClosed = false;
      let released = false;
      let requestTimer;
      let terminationTimer;
      const release = () => {
        if (released) return;
        released = true;
        config.slots.delete(slot);
      };
      const closeLifecycle = () => {
        if (lifecycleClosed) return;
        lifecycleClosed = true;
        clearTimeout(terminationTimer);
        config.children.delete(child);
        release();
        notifyChildrenDrained(config);
      };
      const finish = callback => {
        if (settled) return;
        settled = true;
        clearTimeout(requestTimer);
        callback();
      };
      const terminate = () => {
        if (lifecycleClosed || terminationTimer) return;
        try {
          child.kill('SIGTERM');
        } catch {
          // Keep the slot until close even when signaling fails.
        }
        terminationTimer = setTimeout(() => {
          if (!lifecycleClosed) {
            try {
              child.kill('SIGKILL');
            } catch {
              // A failed kill remains fail-closed: the slot is still held.
            }
          }
        }, CHILD_TERMINATION_GRACE_MS);
      };
      const scheduleErroredChildCleanup = () => {
        if (lifecycleClosed || terminationTimer) return;
        terminationTimer = setTimeout(() => {
          if (!lifecycleClosed) {
            try {
              child.kill('SIGKILL');
            } catch {
              // The child remains counted until close or server disposal.
            }
          }
        }, CHILD_ERROR_FORCE_KILL_MS);
      };
      requestTimer = setTimeout(() => {
        terminate();
        finish(() => rejectCall(new Error('product-bridge-timeout')));
      }, REQUEST_TIMEOUT_MS);
      child.stdout.on('data', chunk => {
        if (settled) return;
        stdout = Buffer.concat([stdout, chunk]);
        if (stdout.byteLength > MAX_RESPONSE_BYTES) {
          terminate();
          finish(() => rejectCall(new Error('product-bridge-response-limit')));
          return;
        }
        const newline = stdout.indexOf(0x0a);
        if (newline === -1) return;
        try {
          const value = JSON.parse(stdout.subarray(0, newline).toString('utf8'));
          if (
            !exactKeys(value, 'error,ok,result,schema_version')
            || value.schema_version !== 'vibapp.product-bridge-response.experimental-v1'
            || typeof value.ok !== 'boolean'
            || (value.error !== null && typeof value.error !== 'string')
          ) throw new Error('product-bridge-response-invalid');
          child.stdout.resume();
          finish(() => resolveCall(value));
        } catch (error) {
          terminate();
          finish(() => rejectCall(error));
        }
      });
      child.stderr.on('data', chunk => {
        stderrBytes += chunk.byteLength;
        if (stderrBytes > MAX_STDERR_BYTES && !settled) {
          terminate();
          finish(() => rejectCall(new Error('product-bridge-stderr-limit')));
        }
      });
      child.on('error', error => {
        finish(() => rejectCall(error));
        if (child.pid === undefined) {
          // A failed spawn has no OS child and may not emit `exit`.
          closeLifecycle();
        } else {
          // `error` also covers failures while a process is still alive. It is
          // not a lifecycle boundary, so retain the slot until `close`.
          scheduleErroredChildCleanup();
        }
      });
      child.once('exit', code => {
        if (!settled) {
          finish(() => rejectCall(new Error(`product-bridge-exit-${code ?? 'signal'}`)));
        }
      });
      child.once('close', code => {
        closeLifecycle();
        if (!settled) {
          finish(() => rejectCall(new Error(`product-bridge-close-${code ?? 'signal'}`)));
        }
      });
      child.stdin.end(JSON.stringify({ command, args }));
    });
  } catch (error) {
    if (!childStarted) config.slots.delete(slot);
    throw error;
  }
}

export async function loadConfig(environment = process.env) {
  const dataDir = await privateDataDirectory(environment.VIBAPP_WEB_PRODUCT_DATA_DIR || '');
  const token = environment.VIBAPP_WEB_PRODUCT_TOKEN || '';
  if (token.length < 32 || token.length > 512 || /\s/.test(token)) {
    throw new Error('VIBAPP_WEB_PRODUCT_TOKEN must be a 32..512 character non-whitespace secret');
  }
  const port = Number(environment.VIBAPP_WEB_PRODUCT_PORT || '3189');
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('invalid product backend port');
  const expectedBuildInputsSha256 = environment.VIBAPP_EXPECTED_DESKTOP_BUILD_INPUTS_SHA256 || '';
  if (!SHA256.test(expectedBuildInputsSha256)) {
    throw new Error('VIBAPP_EXPECTED_DESKTOP_BUILD_INPUTS_SHA256 must be a lowercase SHA-256');
  }
  const bridgeSnapshot = await snapshotExecutable(environment.VIBAPP_PRODUCT_BRIDGE_BIN || '');
  const config = {
    dataDir,
    token,
    port,
    children: new Set(),
    slots: new Set(),
    childDrainWaiters: new Set(),
    bridgeHealth: null,
    disposing: false,
    disposed: false,
    disposePromise: null,
  };
  Object.defineProperty(config, 'bridgeSnapshot', {
    value: bridgeSnapshot,
    enumerable: true,
    writable: false,
    configurable: false,
  });
  try {
    const health = await invokeBridge(config, 'health', {});
    config.bridgeHealth = validateBridgeHealth(health, expectedBuildInputsSha256);
    if (!await waitForChildrenDrained(config, 5_000)) {
      throw new Error('product bridge health process did not close');
    }
    await verifyBridgeSnapshot(bridgeSnapshot);
    return config;
  } catch (error) {
    await disposeProductConfig(config);
    throw error;
  }
}

export function createProductServer(config) {
  if (!config?.bridgeHealth) throw new Error('product bridge health was not validated');
  if (!config.bridgeSnapshot || !OWNED_BRIDGE_SNAPSHOTS.has(config.bridgeSnapshot)) {
    throw new Error('product bridge executable snapshot was not validated');
  }
  const server = createServer(async (request, response) => {
    if (request.method === 'GET' && request.url === '/healthz') {
      try {
        await verifyBridgeSnapshot(config.bridgeSnapshot);
        send(response, 200, {
          status: 'ok',
          service: 'vibapp-web-product-backend',
          bridge: {
            schema_version: config.bridgeHealth.schema_version,
            build_input_receipt: config.bridgeHealth.build_input_receipt,
            contracts: config.bridgeHealth.contracts,
            lifecycle: config.bridgeHealth.lifecycle,
            executable_sha256: config.bridgeSnapshot.sha256,
          },
        });
      } catch {
        send(response, 503, {
          status: 'error',
          service: 'vibapp-web-product-backend',
          bridge: null,
          error: 'bridge-unavailable',
        });
      }
      return;
    }
    if (request.method !== 'POST' || request.url !== '/v1/invoke') {
      send(response, 404, { schema_version: RESPONSE_VERSION, ok: false, result: null, error: 'not-found' });
      return;
    }
    if (
      request.headers.cookie
      || request.headers.origin
      || request.headers['content-type']?.split(';', 1)[0].trim().toLowerCase() !== 'application/json'
      || !authorized(request.headers.authorization, config.token)
    ) {
      send(response, 403, { schema_version: RESPONSE_VERSION, ok: false, result: null, error: 'forbidden' });
      return;
    }
    if (config.slots.size >= MAX_CONCURRENCY) {
      send(response, 503, { schema_version: RESPONSE_VERSION, ok: false, result: null, error: 'busy' });
      return;
    }
    try {
      const raw = await readBody(request);
      const value = JSON.parse(raw.toString('utf8'));
      if (
        !exactKeys(value, 'args,command,schema_version')
        || value.schema_version !== REQUEST_VERSION
        || typeof value.command !== 'string'
        || !COMMANDS.has(value.command)
        || !value.args
        || typeof value.args !== 'object'
        || Array.isArray(value.args)
      ) throw new Error('request-invalid');
      const result = await invokeBridge(config, value.command, value.args);
      send(response, result.ok ? 200 : 400, {
        schema_version: RESPONSE_VERSION,
        ok: result.ok,
        result: result.result,
        error: result.error,
      });
    } catch (error) {
      const message = error instanceof SyntaxError
        ? 'request-json-invalid'
        : String(error.message || 'request-failed').slice(0, 512);
      const bridgeUnavailable = message.startsWith('product bridge executable snapshot')
        || message === 'product-bridge-shutting-down';
      send(response, message === 'product-bridge-busy' || bridgeUnavailable ? 503 : 400, {
        schema_version: RESPONSE_VERSION,
        ok: false,
        result: null,
        error: message === 'product-bridge-busy'
          ? 'busy'
          : bridgeUnavailable
            ? 'bridge-unavailable'
            : message,
      });
    } finally {
      // The bridge may have emitted its admission receipt while its bounded
      // delivery worker is still alive. invokeBridge retains the global slot
      // until that exact child exits, so a fast HTTP response cannot create an
      // unbounded forest of 46-minute bridge/controller processes.
    }
  });
  server.on('close', () => {
    void disposeProductConfig(config).catch(error => {
      config.cleanupError = String(error?.message || error).slice(0, 512);
    });
  });
  return server;
}

export async function startProductServer(config) {
  config ||= await loadConfig();
  const server = createProductServer(config);
  try {
    await new Promise((resolveListen, rejectListen) => {
      server.once('error', rejectListen);
      server.listen(config.port, '127.0.0.1', () => {
        server.off('error', rejectListen);
        resolveListen();
      });
    });
    return server;
  } catch (error) {
    await disposeProductConfig(config);
    throw error;
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const config = await loadConfig();
  const server = await startProductServer(config);
  process.stderr.write(`vibapp web product backend listening on http://127.0.0.1:${config.port}\n`);
  for (const signal of ['SIGINT', 'SIGTERM']) {
    process.on(signal, () => server.close(async () => {
      try {
        await disposeProductConfig(config);
        process.exit(0);
      } catch (error) {
        process.stderr.write(`vibapp web product backend cleanup failed: ${error.message}\n`);
        process.exit(1);
      }
    }));
  }
}
