'use client';

import { useEffect, useRef } from 'react';
import roomHashSync from '../public/roomhash/SYNC.json';

type PreviewFrameProps = {
  appId?: string;
  previewOrigin: string;
  title: string;
  trustedShellOnly?: boolean;
};

const STORAGE_LIMITS = new Map([
  ['vibapp.web-launcher.state.v1', 1024 * 1024],
  ['vibapp.ui_locale', 16],
]);
const MAX_PRODUCT_MESSAGE_BYTES = 512 * 1024;
const MAX_PUBLIC_LOCATOR_INDEX_BYTES = 512 * 1024;
const MAX_PUBLIC_LOCATOR_BYTES = 256 * 1024;
const MAX_PUBLIC_REGISTRY_BYTES = 4 * 1024 * 1024;
const MAX_PUBLIC_PACKAGE_BYTES = 64 * 1024 * 1024;
const MAX_PUBLIC_PACKAGE_FILES = 128;
const MIN_BROWSER_CACHE_MIB = 64;
const MAX_BROWSER_CACHE_MIB = 2_048;
const SHA256 = /^[0-9a-f]{64}$/;
const INFO_HASH = /^[0-9a-f]{40}$/;
const APP_ID = /^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$/;
const SAFE_PACKAGE_PATH = /^(?!\/)(?!.*(?:^|\/)\.{1,2}(?:\/|$))(?!.*\/\/)[A-Za-z0-9._/-]{1,240}$/;
const PUBLIC_LOCATOR_TRUST_NOTE = 'locator-only-package-bytes-require-vibapp-verification';
const PUBLIC_TRACKERS = new Set([
  'wss://tracker.webtorrent.dev',
  'wss://tracker.openwebtorrent.com',
  'wss://tracker.btorrent.xyz',
]);
const PRODUCT_COMMANDS = new Set([
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
  'get_network_status',
  'start_network_node',
  'stop_network_node',
  'create_collaboration_session',
  'join_collaboration_session',
  'leave_collaboration_session',
  'send_collaboration_event',
  'receive_collaboration_events',
  'get_collaboration_status',
  'ensure_public_app_available',
  'submit_development_task',
]);
const BROWSER_NETWORK_COMMANDS = new Set([
  'get_network_status',
  'start_network_node',
  'stop_network_node',
]);
const BROWSER_COLLABORATION_COMMANDS = new Set([
  'create_collaboration_session',
  'join_collaboration_session',
  'leave_collaboration_session',
  'send_collaboration_event',
  'receive_collaboration_events',
  'get_collaboration_status',
]);

type BrowserPackageStore = {
  close: () => unknown | Promise<unknown>;
};

type BrowserRoomHashNode = {
  start: (settings: Record<string, unknown>) => unknown | Promise<unknown>;
  status: () => unknown | Promise<unknown>;
  stop: () => unknown | Promise<unknown>;
  fetchVerifiedCandidateToStore: (input: Record<string, unknown>) => unknown | Promise<unknown>;
};

type BrowserNetworkController = {
  node: BrowserRoomHashNode;
  store: BrowserPackageStore;
  cacheLimitBytes: number;
};

type BrowserRoomHashModule = {
  createRoomHashBrowserNode: (input: { candidateStore: BrowserPackageStore }) => BrowserRoomHashNode;
};

type BrowserPackageStoreModule = {
  createBrowserPackageStore: (input: { cacheLimitBytes: number }) => BrowserPackageStore;
};

let browserRoomHashModule: Promise<BrowserRoomHashModule> | null = null;
let browserPackageStoreModule: Promise<BrowserPackageStoreModule> | null = null;

type BrowserCollaborationBroker = {
  create: (input: Record<string, unknown>) => unknown | Promise<unknown>;
  join: (input: Record<string, unknown>) => unknown | Promise<unknown>;
  leave: (input: Record<string, unknown>) => unknown | Promise<unknown>;
  send: (input: Record<string, unknown>) => unknown | Promise<unknown>;
  receive: (input: Record<string, unknown>) => unknown;
  status: () => unknown;
  close: () => unknown | Promise<unknown>;
};

type BrowserCollaborationModule = {
  createBrowserCollaborationBroker: (input: {
    appId: string;
    settings: Record<string, unknown>;
  }) => Promise<BrowserCollaborationBroker>;
};

let browserCollaborationModule: Promise<BrowserCollaborationModule> | null = null;

async function loadBrowserRoomHash(): Promise<BrowserRoomHashModule> {
  if (!browserRoomHashModule) {
    const version = typeof roomHashSync.adapter_sha256 === 'string'
      ? roomHashSync.adapter_sha256.slice(0, 16)
      : 'invalid';
    const specifier = '/roomhash/roomhash-browser-node.mjs?v=' + version;
    browserRoomHashModule = import(/* @vite-ignore */ specifier) as Promise<BrowserRoomHashModule>;
  }
  return browserRoomHashModule;
}

async function loadBrowserPackageStore(): Promise<BrowserPackageStoreModule> {
  if (!browserPackageStoreModule) {
    const version = typeof roomHashSync.package_store_sha256 === 'string'
      ? roomHashSync.package_store_sha256.slice(0, 16)
      : 'invalid';
    const specifier = '/roomhash/browser-package-store.mjs?v=' + version;
    browserPackageStoreModule = import(/* @vite-ignore */ specifier) as Promise<BrowserPackageStoreModule>;
  }
  return browserPackageStoreModule;
}

async function loadBrowserCollaboration(): Promise<BrowserCollaborationModule> {
  if (!browserCollaborationModule) {
    const version = typeof roomHashSync.browser_collaboration_broker_sha256 === 'string'
      ? roomHashSync.browser_collaboration_broker_sha256.slice(0, 16)
      : 'invalid';
    const specifier = '/roomhash/browser-broker.mjs?v=' + version;
    browserCollaborationModule = import(/* @vite-ignore */ specifier) as Promise<BrowserCollaborationModule>;
  }
  return browserCollaborationModule;
}

function normalizedBrowserNetworkStatus(value: unknown, fallback: string): Record<string, unknown> {
  const raw = value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
  const torrentValue = raw.torrents;
  const torrents = Array.isArray(torrentValue)
    ? torrentValue.length
    : Number.isFinite(Number(torrentValue)) ? Number(torrentValue) : 0;
  const transferValue = raw.activeTransfers ?? raw.active_transfers ?? raw.transfers;
  const activeTransfers = Array.isArray(transferValue)
    ? transferValue.length
    : Number.isFinite(Number(transferValue)) ? Number(transferValue) : 0;
  const running = raw.running === true || raw.started === true || raw.state === 'running';
  return {
    schemaVersion: 'vibapp.network-node-status.experimental-v1',
    state: running ? 'running' : typeof raw.state === 'string' ? raw.state : fallback,
    implementation: 'roomhash-current',
    surface: 'website-browser-foreground',
    foregroundOnly: true,
    activeTransfers,
    torrents,
    lastError: typeof raw.lastError === 'string' ? raw.lastError : null,
    transport: raw,
  };
}

async function invokeProductBackend(command: string, args: Record<string, unknown>): Promise<unknown> {
  const body = JSON.stringify({
    schema_version: 'vibapp.web-product-request.experimental-v1',
    command,
    args,
  });
  const response = await fetch('/api/product', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body,
    cache: 'no-store',
    credentials: 'omit',
    redirect: 'error',
  });
  const result: unknown = await response.json();
  const record = result as Record<string, unknown>;
  if (
    !result
    || typeof result !== 'object'
    || Array.isArray(result)
    || Object.keys(result).sort().join(',') !== 'error,ok,result,schema_version'
    || record.schema_version !== 'vibapp.website-product-route.experimental-v1'
    || record.ok !== true
  ) throw new Error(typeof record.error === 'string' ? record.error : 'product-backend-invalid');
  return record.result;
}

function exactObject(value: unknown, keys: string, reason: string): Record<string, unknown> {
  if (
    !value
    || typeof value !== 'object'
    || Array.isArray(value)
    || Object.keys(value).sort().join(',') !== keys
  ) throw new Error('integrity-failure:' + reason);
  return value as Record<string, unknown>;
}

async function boundedSameOriginJson(path: string, maximumBytes: number, reason: string): Promise<{
  bytes: ArrayBuffer;
  value: unknown;
}> {
  const url = new URL(path, location.origin);
  if (
    url.origin !== location.origin
    || url.pathname !== path
    || url.search !== ''
    || url.hash !== ''
    || url.username !== ''
    || url.password !== ''
  ) throw new Error('integrity-failure:' + reason + '-origin');
  const response = await fetch(url, {
    cache: 'no-store',
    credentials: 'omit',
    redirect: 'error',
  });
  if (!response.ok) throw new Error(reason + '-unavailable');
  const declaredLength = response.headers.get('content-length');
  if (declaredLength !== null) {
    if (!/^(?:0|[1-9][0-9]{0,15})$/.test(declaredLength)) {
      throw new Error('integrity-failure:' + reason + '-content-length');
    }
    const length = Number(declaredLength);
    if (!Number.isSafeInteger(length) || length < 2 || length > maximumBytes) {
      throw new Error('integrity-failure:' + reason + '-size');
    }
  }
  const bytes = await response.arrayBuffer();
  if (bytes.byteLength < 2 || bytes.byteLength > maximumBytes) {
    throw new Error('integrity-failure:' + reason + '-size');
  }
  try {
    return {
      bytes,
      value: JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)),
    };
  } catch {
    throw new Error('integrity-failure:' + reason + '-json');
  }
}

async function sha256Hex(bytes: BufferSource): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
}

function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(canonicalJson).join(',') + ']';
  const item = value as Record<string, unknown>;
  return '{' + Object.keys(item).sort().map(key => JSON.stringify(key) + ':' + canonicalJson(item[key])).join(',') + '}';
}

function browserCacheLimitBytes(settings: Record<string, unknown>): number {
  const configured = Number(settings.cacheLimitMib);
  if (!Number.isSafeInteger(configured) || configured < 128 || configured > 1_048_576) {
    throw new Error('browser-cache-limit-invalid');
  }
  return Math.max(MIN_BROWSER_CACHE_MIB, Math.min(MAX_BROWSER_CACHE_MIB, configured)) * 1024 * 1024;
}

type PublicCandidateLocator = {
  appId: string;
  packageDigestSha256: string;
  webRuntimeAvailable: boolean;
  fileCount: number;
  sizeBytes: number;
  candidate: Record<string, unknown>;
};

async function readPublicCandidateLocator(args: Record<string, unknown>): Promise<PublicCandidateLocator> {
  if (Object.keys(args).sort().join(',') !== 'appId,packageDigestSha256') {
    throw new Error('public-package-request-invalid');
  }
  const appId = args.appId;
  const packageDigestSha256 = args.packageDigestSha256;
  if (
    typeof appId !== 'string'
    || appId.length > 128
    || !APP_ID.test(appId)
    || typeof packageDigestSha256 !== 'string'
    || !SHA256.test(packageDigestSha256)
  ) throw new Error('public-package-request-invalid');

  const [indexDocument, registryDocument] = await Promise.all([
    boundedSameOriginJson(
      '/data/package-locators/index.json',
      MAX_PUBLIC_LOCATOR_INDEX_BYTES,
      'public-package-index',
    ),
    boundedSameOriginJson(
      '/data/registry.snapshot.json',
      MAX_PUBLIC_REGISTRY_BYTES,
      'public-registry-snapshot',
    ),
  ]);
  const index = exactObject(
    indexDocument.value,
    'entries,registry_snapshot_sha256,schema_version,trust_note',
    'public-package-index-shape',
  );
  if (
    index.schema_version !== 'vibapp.public-package-locator-index.experimental-v1'
    || index.trust_note !== PUBLIC_LOCATOR_TRUST_NOTE
    || typeof index.registry_snapshot_sha256 !== 'string'
    || !SHA256.test(index.registry_snapshot_sha256)
    || await sha256Hex(registryDocument.bytes) !== index.registry_snapshot_sha256
    || !Array.isArray(index.entries)
    || index.entries.length > 1_024
  ) throw new Error('integrity-failure:public-package-index-header');

  const registrySnapshot = registryDocument.value;
  if (
    !registrySnapshot
    || typeof registrySnapshot !== 'object'
    || Array.isArray(registrySnapshot)
    || !Array.isArray((registrySnapshot as Record<string, unknown>).records)
    || ((registrySnapshot as Record<string, unknown>).records as unknown[]).length > 1_024
  ) throw new Error('integrity-failure:public-registry-snapshot-shape');

  const observed = new Set<string>();
  let selected: Record<string, unknown> | null = null;
  for (const rawEntry of index.entries) {
    const entry = exactObject(
      rawEntry,
      'app_id,locator_path,locator_sha256,package_digest_sha256,record_id,record_revision,registry_record_sha256,size_bytes,web_runtime_available',
      'public-package-index-entry',
    );
    if (
      typeof entry.app_id !== 'string'
      || entry.app_id.length > 128
      || !APP_ID.test(entry.app_id)
      || typeof entry.record_id !== 'string'
      || entry.record_id.length < 1
      || entry.record_id.length > 256
      || !Number.isSafeInteger(entry.record_revision)
      || Number(entry.record_revision) < 1
      || typeof entry.package_digest_sha256 !== 'string'
      || !SHA256.test(entry.package_digest_sha256)
      || typeof entry.locator_sha256 !== 'string'
      || !SHA256.test(entry.locator_sha256)
      || typeof entry.registry_record_sha256 !== 'string'
      || !SHA256.test(entry.registry_record_sha256)
      || !Number.isSafeInteger(entry.size_bytes)
      || Number(entry.size_bytes) < 2
      || Number(entry.size_bytes) > MAX_PUBLIC_PACKAGE_BYTES
      || typeof entry.web_runtime_available !== 'boolean'
      || entry.locator_path !== `/data/package-locators/${entry.package_digest_sha256}.json`
    ) throw new Error('integrity-failure:public-package-index-entry');
    const identity = `${entry.app_id}:${entry.package_digest_sha256}`;
    if (observed.has(identity)) throw new Error('integrity-failure:public-package-index-duplicate');
    observed.add(identity);
    if (entry.app_id === appId && entry.package_digest_sha256 === packageDigestSha256) selected = entry;
  }
  if (!selected) throw new Error('public-package-locator-unavailable');

  const records = (registrySnapshot as Record<string, unknown>).records as unknown[];
  const matchingRecords = records.filter(rawRecord => {
    if (!rawRecord || typeof rawRecord !== 'object' || Array.isArray(rawRecord)) return false;
    const record = rawRecord as Record<string, unknown>;
    const app = record.app && typeof record.app === 'object' && !Array.isArray(record.app)
      ? record.app as Record<string, unknown> : {};
    const publisher = app.publisher && typeof app.publisher === 'object' && !Array.isArray(app.publisher)
      ? app.publisher as Record<string, unknown> : {};
    const packageRecord = record.package && typeof record.package === 'object' && !Array.isArray(record.package)
      ? record.package as Record<string, unknown> : {};
    const publication = record.publication && typeof record.publication === 'object' && !Array.isArray(record.publication)
      ? record.publication as Record<string, unknown> : {};
    const verification = record.verification && typeof record.verification === 'object' && !Array.isArray(record.verification)
      ? record.verification as Record<string, unknown> : {};
    const source = record.source && typeof record.source === 'object' && !Array.isArray(record.source)
      ? record.source as Record<string, unknown> : {};
    const archive = source.github_archive && typeof source.github_archive === 'object' && !Array.isArray(source.github_archive)
      ? source.github_archive as Record<string, unknown> : {};
    return record.record_id === selected?.record_id
      && record.record_revision === selected?.record_revision
      && record.schema_version === 'vibapp.registry-record.product-v0.0.1'
      && record.document_type === 'registry-record'
      && app.id === appId
      && publisher.verification_state === 'verified'
      && packageRecord.app_id === appId
      && packageRecord.version === app.version
      && packageRecord.package_digest_sha256 === packageDigestSha256
      && publication.state === 'published'
      && publication.revoked_at_utc === null
      && verification.status === 'verified'
      && verification.revocation === 'not-revoked'
      && (source.visibility === 'public' || source.visibility === 'private')
      && archive.organization === 'vib-app'
      && typeof archive.repository === 'string'
      && ((archive.repository === 'sources' && source.visibility === 'public') || /^app-[0-9a-f]{64}$/.test(archive.repository))
      && typeof archive.repository_id === 'number' && Number.isSafeInteger(archive.repository_id) && archive.repository_id > 0
      && typeof archive.commit_sha === 'string' && /^[0-9a-f]{40}$/.test(archive.commit_sha)
      && typeof archive.source_digest_sha256 === 'string' && SHA256.test(archive.source_digest_sha256)
      && archive.source_digest_sha256 === source.source_digest_sha256
      && archive.package_digest_sha256 === packageDigestSha256;
  });
  if (matchingRecords.length !== 1) {
    throw new Error('integrity-failure:public-package-registry-binding');
  }
  const canonicalRecordBytes = new TextEncoder().encode(canonicalJson(matchingRecords[0]));
  if (await sha256Hex(canonicalRecordBytes.buffer) !== selected.registry_record_sha256) {
    throw new Error('integrity-failure:public-package-registry-record-digest');
  }

  const locatorPath = selected.locator_path as string;
  const locatorDocument = await boundedSameOriginJson(locatorPath, MAX_PUBLIC_LOCATOR_BYTES, 'public-package-locator');
  if (await sha256Hex(locatorDocument.bytes) !== selected.locator_sha256) {
    throw new Error('integrity-failure:public-package-locator-digest');
  }
  const locator = exactObject(
    locatorDocument.value,
    'files,info_hash,magnet_uri,package_digest_sha256,schema_version,size_bytes,transport,trust_note',
    'public-package-locator-shape',
  );
  if (
    locator.schema_version !== 'vibapp.roomhash-package-locator.experimental-v1'
    || locator.package_digest_sha256 !== packageDigestSha256
    || locator.transport !== 'bittorrent-v1'
    || typeof locator.info_hash !== 'string'
    || !INFO_HASH.test(locator.info_hash)
    || typeof locator.magnet_uri !== 'string'
    || locator.magnet_uri.length > 4_096
    || locator.trust_note !== PUBLIC_LOCATOR_TRUST_NOTE
    || locator.size_bytes !== selected.size_bytes
    || !Number.isSafeInteger(locator.size_bytes)
    || Number(locator.size_bytes) < 2
    || Number(locator.size_bytes) > MAX_PUBLIC_PACKAGE_BYTES
    || !Array.isArray(locator.files)
    || locator.files.length < 2
    || locator.files.length > MAX_PUBLIC_PACKAGE_FILES
  ) throw new Error('integrity-failure:public-package-locator-header');

  let magnet: URL;
  try {
    magnet = new URL(locator.magnet_uri);
  } catch {
    throw new Error('integrity-failure:public-package-magnet');
  }
  const magnetKeys = [...magnet.searchParams.keys()];
  const topics = magnet.searchParams.getAll('xt');
  const names = magnet.searchParams.getAll('dn');
  const trackers = magnet.searchParams.getAll('tr');
  if (
    magnet.protocol !== 'magnet:'
    || magnet.pathname !== ''
    || magnet.hostname !== ''
    || magnet.username !== ''
    || magnet.password !== ''
    || magnet.hash !== ''
    || magnetKeys.some(key => key !== 'xt' && key !== 'dn' && key !== 'tr')
    || topics.length !== 1
    || topics[0] !== `urn:btih:${locator.info_hash}`
    || names.length !== 1
    || names[0] !== `${packageDigestSha256}.vibapp-candidate`
    || trackers.length < 1
    || trackers.length > PUBLIC_TRACKERS.size
    || new Set(trackers).size !== trackers.length
    || trackers.some(tracker => !PUBLIC_TRACKERS.has(tracker))
  ) throw new Error('integrity-failure:public-package-magnet');

  const paths = new Set<string>();
  let totalBytes = 0;
  const files = locator.files.map((rawFile, indexNumber) => {
    const file = exactObject(rawFile, 'path,sha256,size_bytes', `public-package-file-${indexNumber}`);
    if (
      typeof file.path !== 'string'
      || !SAFE_PACKAGE_PATH.test(file.path)
      || paths.has(file.path)
      || typeof file.sha256 !== 'string'
      || !SHA256.test(file.sha256)
      || !Number.isSafeInteger(file.size_bytes)
      || Number(file.size_bytes) < 1
      || Number(file.size_bytes) > MAX_PUBLIC_PACKAGE_BYTES
    ) throw new Error('integrity-failure:public-package-file');
    paths.add(file.path);
    totalBytes += Number(file.size_bytes);
    if (!Number.isSafeInteger(totalBytes) || totalBytes > MAX_PUBLIC_PACKAGE_BYTES) {
      throw new Error('integrity-failure:public-package-size');
    }
    return { path: file.path, sha256: file.sha256, sizeBytes: file.size_bytes };
  });
  if (
    totalBytes !== locator.size_bytes
    || !paths.has('candidate.json')
    || !paths.has('package/manifest.json')
  ) throw new Error('integrity-failure:public-package-inventory');

  return {
    appId,
    packageDigestSha256,
    webRuntimeAvailable: selected.web_runtime_available as boolean,
    fileCount: files.length,
    sizeBytes: totalBytes,
    candidate: {
      packageDigestSha256,
      infoHash: locator.info_hash,
      magnetURI: locator.magnet_uri,
      files,
      timeoutMs: 120_000,
    },
  };
}

type StorageRequest = {
  schema_version: 'vibapp.web-parent-storage.experimental-v1';
  kind: 'storage-get' | 'storage-set';
  request_id: string;
  key: string;
  value: string | null;
};

type ProductRequest = {
  schema_version: 'vibapp.web-parent-product.experimental-v1';
  kind: 'product-invoke';
  request_id: string;
  command: string;
  args: Record<string, unknown>;
};

function validStorageRequest(value: unknown): value is StorageRequest {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const message = value as Record<string, unknown>;
  if (Object.keys(message).sort().join(',') !== 'key,kind,request_id,schema_version,value') return false;
  if (message.schema_version !== 'vibapp.web-parent-storage.experimental-v1') return false;
  if (message.kind !== 'storage-get' && message.kind !== 'storage-set') return false;
  if (typeof message.request_id !== 'string' || !/^storage-request-[a-f0-9-]{16,96}$/.test(message.request_id)) return false;
  if (typeof message.key !== 'string' || !STORAGE_LIMITS.has(message.key)) return false;
  if (message.kind === 'storage-get') return message.value === null;
  return typeof message.value === 'string'
    && new TextEncoder().encode(message.value).byteLength <= (STORAGE_LIMITS.get(message.key) || 0);
}

function validProductRequest(value: unknown): value is ProductRequest {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const message = value as Record<string, unknown>;
  if (Object.keys(message).sort().join(',') !== 'args,command,kind,request_id,schema_version') return false;
  if (message.schema_version !== 'vibapp.web-parent-product.experimental-v1' || message.kind !== 'product-invoke') return false;
  if (typeof message.request_id !== 'string' || !/^product-request-[a-f0-9-]{16,96}$/.test(message.request_id)) return false;
  if (typeof message.command !== 'string' || !PRODUCT_COMMANDS.has(message.command)) return false;
  if (!message.args || typeof message.args !== 'object' || Array.isArray(message.args)) return false;
  try {
    return new TextEncoder().encode(JSON.stringify(message)).byteLength <= MAX_PRODUCT_MESSAGE_BYTES;
  } catch {
    return false;
  }
}

function randomToken(prefix: string): string {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return prefix + Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
}

function isReadyMessage(value: unknown, expected: {
  channelId: string;
  nonce: string;
  previewOrigin: string;
}): boolean {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const message = value as Record<string, unknown>;
  const keys = Object.keys(message).sort().join(',');
  return keys === 'blockers,channel_id,completed_controls,isolation_state,kind,nonce,preview_origin,schema_version,stage0_activation_eligible'
    && message.schema_version === 'vibapp.preview-frame-ready.experimental-v1'
    && message.kind === 'preview-frame-ready'
    && message.channel_id === expected.channelId
    && message.nonce === expected.nonce
    && message.preview_origin === expected.previewOrigin
    && message.isolation_state === 'partial'
    && message.stage0_activation_eligible === false
    && Array.isArray(message.completed_controls)
    && Array.isArray(message.blockers);
}

export default function PreviewFrame({ appId, previewOrigin, title, trustedShellOnly = false }: PreviewFrameProps) {
  const frameRef = useRef<HTMLIFrameElement>(null);

  useEffect(() => {
    const frame = frameRef.current;
    if (!frame) return;
    const updateDocumentLanguage = () => {
      let preference = 'auto';
      try { preference = localStorage.getItem('vibapp.ui_locale') || 'auto'; } catch { /* Device preference is optional. */ }
      const language = ['zh-CN', 'en-US'].includes(preference)
        ? preference : navigator.languages?.[0] || navigator.language;
      document.documentElement.lang = /^zh(?:[-_]|$)/i.test(language || '') ? 'zh-CN' : 'en';
    };
    updateDocumentLanguage();
    window.addEventListener('languagechange', updateDocumentLanguage);
    const nonce = randomToken('preview-frame-nonce-');
    const channelId = randomToken('preview-frame-channel-');
    // A hosted shell contains only our own GUI, never guest code. Guest execution
    // retains the separate preview origin; this mode also denies Workers in CSP.
    const targetOrigin = trustedShellOnly ? location.origin : previewOrigin;
    const broker = new URL(trustedShellOnly ? '/launcher/index.html' : '/preview.html', targetOrigin);
    if (appId) broker.searchParams.set('app', appId);
    broker.hash = 'nonce=' + encodeURIComponent(nonce);
    const channel = new MessageChannel();
    let networkController: Promise<BrowserNetworkController> | null = null;
    let networkCacheLimitBytes: number | null = null;
    let publicPackageFetchInProgress = false;

    const closeNetworkController = async (): Promise<void> => {
      const pending = networkController;
      networkController = null;
      networkCacheLimitBytes = null;
      if (!pending) return;
      const controller = await pending;
      try {
        await controller.node.stop();
      } finally {
        await controller.store.close();
      }
    };

    const getNetworkController = async (
      settings: Record<string, unknown>,
    ): Promise<BrowserNetworkController> => {
      const cacheLimitBytes = browserCacheLimitBytes(settings);
      if (networkController && networkCacheLimitBytes === cacheLimitBytes) return networkController;
      if (networkController) {
        if (publicPackageFetchInProgress) throw new Error('public-package-fetch-in-progress');
        await closeNetworkController();
      }
      networkCacheLimitBytes = cacheLimitBytes;
      networkController = (async () => {
        const [roomHashModule, storeModule] = await Promise.all([
          loadBrowserRoomHash(),
          loadBrowserPackageStore(),
        ]);
        const store = storeModule.createBrowserPackageStore({ cacheLimitBytes });
        try {
          const node = roomHashModule.createRoomHashBrowserNode({ candidateStore: store });
          return { node, store, cacheLimitBytes };
        } catch (error) {
          await store.close();
          throw error;
        }
      })();
      networkController.catch(() => {
        networkController = null;
        networkCacheLimitBytes = null;
      });
      return networkController;
    };

    const invokeBrowserNetwork = async (command: string): Promise<Record<string, unknown>> => {
      if (command === 'get_network_status') {
        if (!networkController) return normalizedBrowserNetworkStatus(null, 'stopped');
        const controller = await networkController;
        return normalizedBrowserNetworkStatus(await controller.node.status(), 'stopped');
      }
      if (command === 'stop_network_node') {
        if (publicPackageFetchInProgress) throw new Error('public-package-fetch-in-progress');
        if (!networkController) return normalizedBrowserNetworkStatus(null, 'stopped');
        const controller = await networkController;
        return normalizedBrowserNetworkStatus(await controller.node.stop(), 'stopped');
      }
      if (command !== 'start_network_node') throw new Error('network-command-unsupported');
      const settings = await invokeProductBackend('get_network_settings', {}) as Record<string, unknown>;
      if (settings.p2pEnabled !== true) throw new Error('p2p-disabled-in-settings');
      const controller = await getNetworkController(settings);
      const result = await controller.node.start(settings);
      return normalizedBrowserNetworkStatus(result ?? await controller.node.status(), 'running');
    };

    const ensurePublicAppAvailable = async (args: Record<string, unknown>): Promise<Record<string, unknown>> => {
      if (publicPackageFetchInProgress) throw new Error('public-package-fetch-in-progress');
      const settings = await invokeProductBackend('get_network_settings', {}) as Record<string, unknown>;
      if (settings.p2pEnabled !== true) throw new Error('p2p-disabled-in-settings');
      const locator = await readPublicCandidateLocator(args);
      const controller = await getNetworkController(settings);
      await controller.node.start(settings);
      publicPackageFetchInProgress = true;
      try {
        const rawReceipt = await controller.node.fetchVerifiedCandidateToStore(locator.candidate);
        const receipt = exactObject(
          rawReceipt,
          'committedAtUnixMs,fileCount,packageDigestSha256,schemaVersion,sizeBytes,state',
          'public-package-cache-receipt',
        );
        if (
          receipt.schemaVersion !== 'vibapp.browser-package-cache-receipt.experimental-v1'
          || receipt.packageDigestSha256 !== locator.packageDigestSha256
          || receipt.state !== 'committed'
          || receipt.fileCount !== locator.fileCount
          || receipt.sizeBytes !== locator.sizeBytes
          || !Number.isSafeInteger(receipt.committedAtUnixMs)
          || Number(receipt.committedAtUnixMs) < 0
        ) throw new Error('integrity-failure:public-package-cache-receipt');
        return {
          schema_version: 'vibapp.website-public-package-cache.experimental-v1',
          state: 'cached',
          app_id: locator.appId,
          package_digest_sha256: locator.packageDigestSha256,
          file_count: locator.fileCount,
          size_bytes: locator.sizeBytes,
          committed_at_unix_ms: receipt.committedAtUnixMs,
          web_runtime_available: locator.webRuntimeAvailable,
        };
      } finally {
        publicPackageFetchInProgress = false;
      }
    };

    let collaborationBroker: Promise<BrowserCollaborationBroker> | null = null;
    const getCollaborationBroker = async (): Promise<BrowserCollaborationBroker> => {
      if (!appId) throw new Error('collaboration-requires-an-active-app');
      if (!collaborationBroker) {
        collaborationBroker = (async () => {
          const settings = await invokeProductBackend('get_network_settings', {}) as Record<string, unknown>;
          if (settings.p2pEnabled !== true) throw new Error('p2p-disabled-in-settings');
          const collaborationModule = await loadBrowserCollaboration();
          return collaborationModule.createBrowserCollaborationBroker({ appId, settings });
        })();
        collaborationBroker.catch(() => { collaborationBroker = null; });
      }
      return collaborationBroker;
    };
    const invokeBrowserCollaboration = async (
      command: string,
      args: Record<string, unknown>,
    ): Promise<unknown> => {
      if (command === 'get_collaboration_status' && !collaborationBroker) {
        return {
          state: 'stopped',
          appId: appId ?? null,
          sessionCount: 0,
          queuedEvents: 0,
          queuedBytes: 0,
          sessions: [],
        };
      }
      const broker = await getCollaborationBroker();
      switch (command) {
        case 'create_collaboration_session': {
          if (Object.keys(args).sort().join(',') !== 'appId,confirmed,expiresInMs' || args.appId !== appId) {
            throw new Error('collaboration-create-request-invalid');
          }
          return broker.create({ confirmed: args.confirmed, expiresInMs: args.expiresInMs });
        }
        case 'join_collaboration_session': {
          if (Object.keys(args).sort().join(',') !== 'appId,channelId,confirmed,expiresInMs' || args.appId !== appId) {
            throw new Error('collaboration-join-request-invalid');
          }
          return broker.join({ channelId: args.channelId, confirmed: args.confirmed, expiresInMs: args.expiresInMs });
        }
        case 'leave_collaboration_session': return broker.leave(args);
        case 'send_collaboration_event': {
          if (Object.keys(args).sort().join(',') !== 'payload,sessionId') throw new Error('collaboration-send-request-invalid');
          return broker.send({ ...args, eventId: crypto.randomUUID() });
        }
        case 'receive_collaboration_events': return broker.receive(args);
        case 'get_collaboration_status': return broker.status();
        default: throw new Error('collaboration-command-unsupported');
      }
    };
    let readyConsumed = trustedShellOnly;
    const onPortMessage = async (event: MessageEvent) => {
      if (!readyConsumed) {
        if (!isReadyMessage(event.data, { channelId, nonce, previewOrigin })) return;
        readyConsumed = true;
        frame.dataset.previewBound = 'true';
        return;
      }
      const request = event.data;
      if (validProductRequest(request)) {
        let value: unknown = null;
        let error: string | null = null;
        try {
          if (BROWSER_NETWORK_COMMANDS.has(request.command)) {
            value = await invokeBrowserNetwork(request.command);
          } else if (BROWSER_COLLABORATION_COMMANDS.has(request.command)) {
            value = await invokeBrowserCollaboration(request.command, request.args);
          } else if (request.command === 'ensure_public_app_available') {
            value = await ensurePublicAppAvailable(request.args);
          } else {
            value = await invokeProductBackend(request.command, request.args);
            if (request.command === 'save_network_settings') {
              value = await invokeProductBackend('get_network_settings', {});
              const settings = value as Record<string, unknown>;
              await invokeBrowserNetwork('stop_network_node');
              if (settings.p2pEnabled === true) await invokeBrowserNetwork('start_network_node');
            }
          }
        } catch (failure) {
          error = failure instanceof Error ? failure.message.slice(0, 512) : 'product-backend-unavailable';
        }
        channel.port1.postMessage({
          schema_version: 'vibapp.web-parent-product-result.experimental-v1',
          kind: 'product-result',
          request_id: request.request_id,
          value,
          error,
        });
        return;
      }
      if (!validStorageRequest(request)) return;
      let value: string | null = null;
      let error: 'storage-unavailable' | null = null;
      try {
        if (request.kind === 'storage-get') {
          value = localStorage.getItem(request.key);
          const maximum = STORAGE_LIMITS.get(request.key) || 0;
          if (value !== null && new TextEncoder().encode(value).byteLength > maximum) {
            localStorage.removeItem(request.key);
            value = null;
            error = 'storage-unavailable';
          }
        } else {
          if (request.value === null) throw new Error('storage-value-invalid');
          localStorage.setItem(request.key, request.value);
          if (request.key === 'vibapp.ui_locale') updateDocumentLanguage();
        }
      } catch {
        value = null;
        error = 'storage-unavailable';
      }
      channel.port1.postMessage({
        schema_version: 'vibapp.web-parent-storage-result.experimental-v1',
        kind: 'storage-result',
        request_id: request.request_id,
        value,
        error,
      });
    };
    channel.port1.onmessage = event => { void onPortMessage(event); };
    channel.port1.start();
    const bind = () => {
      frame.contentWindow?.postMessage(trustedShellOnly ? {
        schema_version: 'vibapp.web-storage-bind.experimental-v1',
        kind: 'bind-web-storage',
      } : {
        schema_version: 'vibapp.preview-frame-bind.experimental-v1',
        kind: 'bind-preview-frame',
        channel_id: channelId,
        nonce,
        preview_origin: previewOrigin,
      }, targetOrigin, [channel.port2]);
    };
    frame.addEventListener('load', bind, { once: true });
    frame.src = broker.toString();
    void invokeProductBackend('get_network_settings', {})
      .then(settings => (settings as Record<string, unknown>).p2pEnabled === true
        ? invokeBrowserNetwork('start_network_node')
        : null)
      .catch(() => null);
    return () => {
      window.removeEventListener('languagechange', updateDocumentLanguage);
      frame.removeEventListener('load', bind);
      channel.port1.close();
      void closeNetworkController().catch(() => {});
      if (collaborationBroker) void collaborationBroker.then(broker => broker.close()).catch(() => {});
    };
  }, [appId, previewOrigin, trustedShellOnly]);

  return (
    <iframe
      {...(trustedShellOnly ? {} : { credentialless: '' })}
      ref={frameRef}
      className="web-client-frame"
      title={title}
      sandbox={trustedShellOnly
        ? 'allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-downloads allow-top-navigation-to-custom-protocols'
        : 'allow-scripts allow-same-origin allow-forms'}
      referrerPolicy="no-referrer"
    />
  );
}
