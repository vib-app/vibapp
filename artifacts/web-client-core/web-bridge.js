import {
  classifyInvalidBootstrapError,
  reportInvalidBootstrap,
} from './invalid-bootstrap-audit.mjs';
import {
  verifyBrowserPreviewBinding,
  verifyDerivationAttestation,
  verifyDerivationFileSet,
  verifyLaunchControl,
  verifyRuntimeResult,
} from './runtime-integrity.mjs';
import { ForegroundClient } from './foreground-session.mjs';

const STATE_KEY = 'vibapp.web-launcher.state.v1';
const LOCALE_KEY = 'vibapp.ui_locale';
const MAX_STATE_BYTES = 1024 * 1024;
const MAX_REGISTRY_BYTES = 4 * 1024 * 1024;
const MAX_LOCATOR_INDEX_BYTES = 512 * 1024;
const SHA256 = /^[0-9a-f]{64}$/;
const encoder = new TextEncoder();
const hostedShellOnly = document.documentElement.dataset.vibappHostedShell === 'true';

let wasm;
let registry;
let publicLocatorIndex = { entries: [] };
let parentStoragePort;
let foregroundSession = null;
let foregroundAppId = null;
addEventListener('pagehide', () => { foregroundSession?.close(); foregroundSession = null; foregroundAppId = null; });
const parentStorageRequests = new Map();
const parentProductRequests = new Map();

function exactKeys(value, expected) {
  return value && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).sort().join(',') === expected;
}

const parentStorageBinding = new Promise((resolve, reject) => {
  let consumed = false;
  const timeout = setTimeout(() => reject(new Error('Web 父页面存储通道连接超时。')), 4_000);
  const bind = event => {
    if (
      consumed
      || event.source !== parent
      || event.origin !== location.origin
      || event.ports.length !== 1
      || !exactKeys(event.data, 'kind,schema_version')
      || event.data.schema_version !== 'vibapp.web-storage-bind.experimental-v1'
      || event.data.kind !== 'bind-web-storage'
    ) return;
    consumed = true;
    clearTimeout(timeout);
    removeEventListener('message', bind);
    parentStoragePort = event.ports[0];
    parentStoragePort.onmessage = responseEvent => {
      const response = responseEvent.data;
      if (!exactKeys(response, 'error,kind,request_id,schema_version,value') || typeof response.request_id !== 'string') return;
      if (
        response.schema_version === 'vibapp.web-parent-storage-result.experimental-v1'
        && response.kind === 'storage-result'
      ) {
        const pending = parentStorageRequests.get(response.request_id);
        if (!pending) return;
        parentStorageRequests.delete(response.request_id);
        clearTimeout(pending.timeout);
        if (response.error !== null) pending.reject(new Error('父页面存储不可用。'));
        else pending.resolve(response.value);
        return;
      }
      if (
        response.schema_version === 'vibapp.web-parent-product-result.experimental-v1'
        && response.kind === 'product-result'
      ) {
        const pending = parentProductRequests.get(response.request_id);
        if (!pending) return;
        parentProductRequests.delete(response.request_id);
        clearTimeout(pending.timeout);
        if (typeof response.error === 'string') pending.reject(new Error(response.error));
        else if (response.error !== null) pending.reject(new Error('产品后端返回了无效错误。'));
        else pending.resolve(response.value);
      }
    };
    parentStoragePort.start();
    resolve();
  };
  addEventListener('message', bind);
});

async function parentStorage(kind, key, value = null) {
  await parentStorageBinding;
  const requestId = 'storage-request-' + crypto.randomUUID();
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      parentStorageRequests.delete(requestId);
      reject(new Error('父页面存储请求超时。'));
    }, 4_000);
    parentStorageRequests.set(requestId, { resolve, reject, timeout });
    parentStoragePort.postMessage({
      schema_version: 'vibapp.web-parent-storage.experimental-v1',
      kind,
      request_id: requestId,
      key,
      value,
    });
  });
}

async function getParentStorage(key) {
  const value = await parentStorage('storage-get', key);
  if (value !== null && typeof value !== 'string') throw new Error('父页面存储返回类型无效。');
  return value;
}

async function setParentStorage(key, value) {
  if (typeof value !== 'string') throw new Error('父页面存储只接受文本。');
  await parentStorage('storage-set', key, value);
}

// Boot asks for several settings at once, while the host deliberately admits
// only four bridge processes (including persistent development workers). Queue
// this page's calls instead of turning that startup burst into missing settings.
// This never retries a dispatched operation or increases host process limits.
let parentProductQueueTail = Promise.resolve();
let parentProductQueueSize = 0;
function parentProduct(command, args = {}) {
  if (parentProductQueueSize >= 16) return Promise.reject(new Error('产品请求队列已满，请稍后再试。'));
  const deadline = Date.now() + 80_000;
  parentProductQueueSize++;
  const pending = parentProductQueueTail.then(async () => {
    await parentStorageBinding;
    const remaining = deadline - Date.now();
    if (remaining <= 0) throw new Error('产品后端请求超时。');
    const requestId = 'product-request-' + crypto.randomUUID();
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        parentProductRequests.delete(requestId);
        reject(new Error('产品后端请求超时。'));
      }, remaining);
      parentProductRequests.set(requestId, { resolve, reject, timeout });
      try {
        parentStoragePort.postMessage({
          schema_version: 'vibapp.web-parent-product.experimental-v1',
          kind: 'product-invoke',
          request_id: requestId,
          command,
          args,
        });
      } catch (error) {
        clearTimeout(timeout);
        parentProductRequests.delete(requestId);
        reject(error);
      }
    });
  });
  parentProductQueueTail = pending.catch(() => {});
  return pending.finally(() => { parentProductQueueSize--; });
}

const storageReady = parentStorageBinding.then(async () => {
  const locale = await getParentStorage(LOCALE_KEY);
  if (locale === 'auto' || locale === 'zh-CN' || locale === 'en-US') localStorage.setItem(LOCALE_KEY, locale);
  else localStorage.removeItem(LOCALE_KEY);
});

const defaults = {
  needs: [],
  jobs: [],
  installIntents: [],
  codeAgentSettings: {
    selectedProvider: 'codex',
    modelByProvider: { codex: 'gpt-5.6-sol' },
  },
  modelSettings: {
    generation: {
      enabled: false,
      baseUrl: 'https://example.invalid/v1',
      model: 'bring-your-own-model',
      protocol: 'responses',
      timeoutSeconds: 45,
      maxOutputTokens: 512,
      temperature: 0,
      hasApiKey: false,
    },
    embedding: {
      enabled: false,
      baseUrl: 'https://example.invalid/v1',
      model: 'bring-your-own-embedding',
      dimensions: 384,
      timeoutSeconds: 5,
      hasApiKey: false,
    },
  },
};

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

async function readState() {
  try {
    const raw = await getParentStorage(STATE_KEY);
    if (!raw || raw.length > MAX_STATE_BYTES) return clone(defaults);
    const value = JSON.parse(raw);
    return {
      needs: Array.isArray(value.needs) ? value.needs.slice(-100) : [],
      jobs: Array.isArray(value.jobs) ? value.jobs.slice(-64) : [],
      installIntents: Array.isArray(value.installIntents) ? value.installIntents.slice(-64) : [],
      codeAgentSettings: clone(defaults.codeAgentSettings),
      modelSettings: value.modelSettings || clone(defaults.modelSettings),
    };
  } catch {
    return clone(defaults);
  }
}

async function writeState(state) {
  const raw = JSON.stringify(state);
  if (encoder.encode(raw).byteLength > MAX_STATE_BYTES) throw new Error('浏览器本地状态超过 1 MiB 上限。');
  await setParentStorage(STATE_KEY, raw);
}

async function initialize() {
  if (wasm && registry) return;
  const [wasmResponse, registryResponse, locatorResponse] = await Promise.all([
    fetch('./vibapp_web_client_core.wasm', { cache: 'no-store', credentials: 'same-origin' }),
    fetch('/data/registry.snapshot.json', { cache: 'no-store', credentials: 'same-origin' }),
    fetch('/data/package-locators/index.json', { cache: 'no-store', credentials: 'same-origin' }),
  ]);
  if (!wasmResponse.ok || !registryResponse.ok) throw new Error('Web GUI 核心或 AppStore 数据不可用。');
  const [bytes, registryBytes] = await Promise.all([
    wasmResponse.arrayBuffer(),
    registryResponse.arrayBuffer(),
  ]);
  if (registryBytes.byteLength < 2 || registryBytes.byteLength > MAX_REGISTRY_BYTES) {
    throw new Error('AppStore 数据超出浏览器支持上限。');
  }
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  const sha256 = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
  const webCore = await WebAssembly.instantiate(bytes, {});
  if (webCore.instance.exports.vibapp_web_abi_version() !== 1) throw new Error('Web GUI Wasm ABI 不兼容。');
  wasm = { exports: webCore.instance.exports, sha256 };
  try {
    registry = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(registryBytes));
  } catch {
    throw new Error('AppStore 数据不是有效 JSON。');
  }
  publicLocatorIndex = { entries: [] };
  if (locatorResponse.ok) {
    const locatorBytes = await locatorResponse.arrayBuffer();
    if (locatorBytes.byteLength >= 2 && locatorBytes.byteLength <= MAX_LOCATOR_INDEX_BYTES) {
      try {
        const candidate = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(locatorBytes));
        const registryDigest = await crypto.subtle.digest('SHA-256', registryBytes);
        const registrySha256 = Array.from(new Uint8Array(registryDigest), byte => byte.toString(16).padStart(2, '0')).join('');
        if (
          exactKeys(candidate, 'entries,registry_snapshot_sha256,schema_version,trust_note')
          && candidate.schema_version === 'vibapp.public-package-locator-index.experimental-v1'
          && candidate.trust_note === 'locator-only-package-bytes-require-vibapp-verification'
          && candidate.registry_snapshot_sha256 === registrySha256
          && Array.isArray(candidate.entries)
          && candidate.entries.length <= 1024
        ) publicLocatorIndex = candidate;
      } catch {
        // Package discovery remains fail-closed while the rest of AppStore stays usable.
      }
    }
  }
}

function shortPermission(value) {
  return String(value).replace('vibapp:experimental-v0/', '').replace('@0.0.1', '');
}

function isPublicRegistryRecord(record) {
  return record
    && record.document_type === 'registry-record'
    && record.publication?.state === 'published'
    && record.verification?.status === 'verified'
    && record.verification?.revocation === 'not-revoked'
    && ['private', 'public'].includes(record.source?.visibility)
    && record.source?.github_archive?.organization === 'vib-app'
    && /^app-[0-9a-f]{64}$/.test(record.source?.github_archive?.repository || '')
    && Number.isSafeInteger(record.source?.github_archive?.repository_id)
    && record.source.github_archive.repository_id > 0
    && /^[0-9a-f]{40}$/.test(record.source?.github_archive?.commit_sha || '')
    && SHA256.test(record.source?.github_archive?.source_digest_sha256 || '')
    && record.source.github_archive.source_digest_sha256 === record.source.source_digest_sha256
    && record.source.github_archive.package_digest_sha256 === record.package?.package_digest_sha256
    && typeof record.app?.id === 'string'
    && record.package?.app_id === record.app.id
    && SHA256.test(record.package?.package_digest_sha256 || '');
}

function isLoopbackLocalProjection() {
  return registry?.consumer_notes?.website_projection_mode === 'loopback-local-development'
    && registry?.consumer_notes?.local_private_preview_enabled === true
    && location.protocol === 'http:'
    && location.hostname === '127.0.0.1'
    && location.port !== '';
}

function isLoopbackPrivatePreviewRecord(record) {
  return isLoopbackLocalProjection()
    && record?.schema_version === 'vibapp.browser-preview-record.experimental-v1'
    && record?.document_type === 'browser-preview-record-projection'
    && record?.publication?.state === 'private-candidate'
    && record?.verification?.status === 'locally-derived-awaiting-independent-verifier';
}

function isLoopbackPrivateBinding(record, binding) {
  return isLoopbackPrivatePreviewRecord(record)
    && binding?.schema_version === 'vibapp.browser-derivation-binding.experimental-v1'
    && binding?.source_kind === 'verifier-promoted-candidate'
    && binding?.app_id === record.app.id
    && binding?.profile === 'web-preview'
    && binding?.canonical_package_digest_sha256 === record.package.package_digest_sha256
    && binding?.entry?.format === 'jco-esm'
    && Array.isArray(binding?.files)
    && binding.files.some(item => item.path === binding.entry.path && item.sha256 === binding.entry.sha256)
    && binding?.attestation?.verification_state === 'locally-derived-awaiting-independent-verifier'
    && binding?.attestation?.canonical_component_transformation_proven === true
    && binding?.attestation?.stage0_activation_eligible === false;
}

function validPublicBrowserDescriptor(value, expectedPrefix) {
  return value
    && typeof value === 'object'
    && !Array.isArray(value)
    && typeof value.path === 'string'
    && value.path.startsWith(expectedPrefix)
    && !value.path.split('/').some((part, index) => index > 0 && (part === '' || part === '.' || part === '..'))
    && typeof value.media_type === 'string'
    && SHA256.test(value.sha256 || '')
    && Number.isInteger(value.size_bytes)
    && value.size_bytes > 0
    && value.size_bytes <= 4 * 1024 * 1024;
}

function isRealPublicBrowserBinding(record, binding) {
  if (!isPublicRegistryRecord(record) || !binding || typeof binding !== 'object' || Array.isArray(binding)) return false;
  const prefix = '/launcher/components/' + String(binding.canonical_component?.sha256 || '') + '/';
  const files = Array.isArray(binding.files) ? binding.files : [];
  return binding.schema_version === 'vibapp.browser-derivation-binding.experimental-v1'
    && binding.source_kind === 'verifier-promoted-candidate'
    && binding.app_id === record.app.id
    && binding.canonical_package_digest_sha256 === record.package.package_digest_sha256
    && (binding.profile === 'web-preview' || binding.profile === 'web-runtime')
    && Array.isArray(record.compatibility?.profiles)
    && record.compatibility.profiles.includes(binding.profile)
    && binding.canonical_component?.media_type === 'application/wasm'
    && SHA256.test(binding.canonical_component?.sha256 || '')
    && binding.derived_from_sha256 === binding.canonical_component.sha256
    && binding.entry?.format === 'jco-esm'
    && validPublicBrowserDescriptor(binding.entry, prefix)
    && files.length >= 1
    && files.length <= 32
    && files.every(item => typeof item?.format === 'string' && validPublicBrowserDescriptor(item, prefix))
    && files.some(item => item.path === binding.entry.path && item.sha256 === binding.entry.sha256)
    && binding.attestation?.verification_state === 'verified'
    && binding.attestation?.canonical_component_transformation_proven === true
    && binding.attestation?.stage0_activation_eligible === true
    && SHA256.test(binding.attestation?.binding_payload_sha256 || '')
    && validPublicBrowserDescriptor(binding.attestation?.artifact, prefix);
}

function browserBindingForRecord(record) {
  return (registry.browser_artifact_bindings || []).find(binding => (
    isRealPublicBrowserBinding(record, binding) || (!hostedShellOnly && isLoopbackPrivateBinding(record, binding))
  ));
}

function publicLocatorForRecord(record) {
  if (!isPublicRegistryRecord(record)) return null;
  const matches = (publicLocatorIndex.entries || []).filter(entry => (
    exactKeys(entry, 'app_id,locator_path,locator_sha256,package_digest_sha256,record_id,record_revision,registry_record_sha256,size_bytes,web_runtime_available')
    && entry.app_id === record.app.id
    && entry.record_id === record.record_id
    && entry.record_revision === record.record_revision
    && entry.package_digest_sha256 === record.package.package_digest_sha256
    && entry.locator_path === `/data/package-locators/${record.package.package_digest_sha256}.json`
    && SHA256.test(entry.locator_sha256 || '')
    && SHA256.test(entry.registry_record_sha256 || '')
    && Number.isInteger(entry.size_bytes)
    && entry.size_bytes >= 2
    && entry.size_bytes <= 64 * 1024 * 1024
    && typeof entry.web_runtime_available === 'boolean'
  ));
  return matches.length === 1 ? matches[0] : null;
}

function allRecords() {
  const publicRecords = (registry.records || []).filter(isPublicRegistryRecord);
  const localRecords = isLoopbackLocalProjection()
    ? (registry.browser_preview_records || []).filter(record => (
        isLoopbackPrivatePreviewRecord(record) && browserBindingForRecord(record)
      ))
    : [];
  return [...publicRecords, ...localRecords];
}

function appRecord(record) {
  const webProfile = record.compatibility.profiles.find(profile => profile === 'web-preview' || profile === 'web-runtime');
  const browserBinding = browserBindingForRecord(record);
  const publicBinding = browserBinding && isRealPublicBrowserBinding(record, browserBinding);
  const publicLocator = publicLocatorForRecord(record);
  const publicRecord = isPublicRegistryRecord(record);
  return {
    app_id: record.app.id,
    display_name: record.app.display_name,
    version: record.app.version,
    kind: record.app.kind,
    summary: record.app.summary,
    publisher: record.app.publisher.display_name,
    package_digest_sha256: record.package.package_digest_sha256,
    component_sha256: null,
    browser_artifact_sha256: browserBinding?.entry?.sha256 || null,
    permissions: record.permissions.map(item => shortPermission(item.interface)),
    verification_state: record.verification.status,
    verification_summary: publicBinding
      ? '公开浏览器运行物已绑定 Registry 记录、canonical Component、派生证明和逐文件摘要；P2P 包缓存仍会独立复验。'
      : browserBinding
        ? '已从 verifier-promoted canonical Component 用固定 Jco 版本本地派生并逐字节校验；浏览器派生仍等待独立验证，且原 manifest 尚未声明 Web 激活资格。'
      : 'Registry 元数据已验证；安装和运行权限仍由对应宿主决定。',
    publication_state: record.publication.state,
    publication_badge: record.publication.state === 'published' ? 'public-appstore' : 'private',
    install_eligible: Boolean(publicRecord && publicLocator),
    web_cache_eligible: Boolean(publicRecord && publicLocator),
    web_runtime_available: Boolean(publicBinding && publicLocator?.web_runtime_available === true),
    launch_eligible: Boolean(browserBinding && (!hostedShellOnly || (publicBinding && publicLocator?.web_runtime_available === true))),
    launch_mode: browserBinding ? 'web-worker-foreground' : 'client-required',
    web_unavailable_reason: browserBinding ? null : 'browser-runtime-unavailable',
    installation_state: publicRecord && publicLocator ? 'candidate' : 'not-installed',
    active_profile: webProfile || record.compatibility.profiles[0],
  };
}

function webApps(productApps, installIntents) {
  const appMap = new Map(allRecords().map(record => {
    const app = appRecord(record);
    return [app.app_id, app];
  }));
  for (const app of Array.isArray(productApps) ? productApps : []) {
    if (!app || typeof app !== 'object' || Array.isArray(app) || typeof app.app_id !== 'string'
      || !app.app_id || appMap.has(app.app_id)) continue;
    // Product feeds describe the desktop host. They cannot grant browser
    // activation, private installation, native lifecycle or service authority.
    appMap.set(app.app_id, {
      ...app,
      install_eligible: false,
      launch_eligible: false,
      launch_mode: 'client-required',
      installation_state: 'client-required',
      active_profile: null,
      web_cache_eligible: false,
      web_package_cached: false,
      web_runtime_available: false,
      web_unavailable_reason: 'browser-runtime-unavailable',
      update_eligible: false,
      available_update: null,
      update: null,
      active_service_entrypoints: [],
      service_statuses: {},
      guest_execution_performed: false,
    });
  }
  for (const intent of Array.isArray(installIntents) ? installIntents : []) {
    if (intent?.web_package_cached !== true || intent?.status !== 'cached-for-web') continue;
    const app = appMap.get(intent.app_id);
    // A retained receipt never upgrades a desktop-only app or stale/revoked
    // Registry row, nor supplies a missing verified browser binding.
    if (!app || app.web_cache_eligible !== true || app.package_digest_sha256 !== intent.package_digest_sha256) continue;
    appMap.set(intent.app_id, {
      ...app,
      installation_state: 'cached',
      web_package_cached: true,
      web_runtime_available: app.web_runtime_available === true && intent.web_runtime_available === true,
    });
  }
  return [...appMap.values()];
}

async function submitNeed(payload) {
  const count = Array.from(String(payload.description || '')).length;
  if (wasm.exports.vibapp_accept_need(count) !== 1) throw new Error('需求描述需为 10–2000 个字符。');
  const result = await parentProduct('submit_need', payload);
  const state = await readState();
  state.needs.push({ ...result.need, updated_at_utc: new Date().toISOString() });
  await writeState(state);
  return result;
}

async function completeNeed(payload) {
  const result = await parentProduct('complete_need', payload);
  const state = await readState();
  const previous = state.needs.find(item => item.need_id === payload.need_id);
  if (!previous) throw new Error('需求草稿不存在或已过期。');
  state.needs = state.needs.map(item => item.need_id === payload.need_id ? { ...item, status: 'complete' } : item);
  await writeState(state);
  return result;
}

async function launchApp(appId) {
  foregroundSession?.close(); foregroundSession = null; foregroundAppId = null;
  const record = allRecords().find(item => item.app.id === appId);
  const binding = record ? browserBindingForRecord(record) : null;
  if (!record || !binding) {
    throw new Error('这个应用没有可验证的浏览器前台派生物；请在 VibApp Client 中运行。');
  }
  if (hostedShellOnly && (!isRealPublicBrowserBinding(record, binding)
    || publicLocatorForRecord(record)?.web_runtime_available !== true)) {
    throw new Error('This app has no verified public browser runtime. Open it in VibApp.');
  }
  await verifyBrowserPreviewBinding(record, binding);
  const attestationUrl = new URL(binding.attestation.artifact.path, location.origin);
  if (attestationUrl.origin !== location.origin || !attestationUrl.pathname.startsWith('/launcher/components/')) {
    throw new Error('integrity-failure:attestation-origin');
  }
  const fileValues = new Map();
  for (const descriptor of binding.files) {
    const fileUrl = new URL(descriptor.path, location.origin);
    if (fileUrl.origin !== location.origin || !fileUrl.pathname.startsWith('/launcher/components/')) {
      throw new Error('integrity-failure:derivation-file-origin');
    }
    const response = await fetch(fileUrl, { cache: 'no-store', credentials: 'omit', redirect: 'error' });
    if (!response.ok) throw new Error('浏览器派生物不可用。');
    fileValues.set(descriptor.path, await response.arrayBuffer());
  }
  await verifyDerivationFileSet(binding, fileValues);
  const attestationResponse = await fetch(attestationUrl, { cache: 'no-store', credentials: 'omit', redirect: 'error' });
  if (!attestationResponse.ok) throw new Error('浏览器派生证明不可用。');
  const attestationBytes = await attestationResponse.arrayBuffer();
  await verifyDerivationAttestation(binding, attestationBytes);
  const session = 'web-session-' + crypto.randomUUID();
  const request = {
    schema_version: 'vibapp.web-worker-launch.experimental-v1',
    app_id: appId,
    session,
    profile: binding.profile,
    package_digest_sha256: binding.canonical_package_digest_sha256,
    component_sha256: binding.canonical_component.sha256,
    browser_artifact_sha256: binding.entry.sha256,
    entry_path: binding.entry.path,
    binding,
  };
  return new Promise((resolve, reject) => {
    const worker = new Worker('./app-runtime-worker.js', { type: 'module', name: 'vibapp-app-runtime' });
    const channel = new MessageChannel();
    const control = {
      schema_version: 'vibapp.preview-control.experimental-v1',
      user_id: 'anonymous-web-preview',
      app_id: appId,
      artifact_digest: binding.entry.sha256,
      generation_id: 'web-generation-' + binding.canonical_component.sha256.slice(0, 24),
      session_id: session,
      view_revision: 1,
      event_id: 'preview-event-' + crypto.randomUUID(),
      channel_id: 'preview-channel-' + crypto.randomUUID(),
      nonce: 'preview-nonce-' + crypto.randomUUID(),
      request_id: 'preview-request-' + crypto.randomUUID(),
      sequence: 1,
      kind: 'launch',
    };
    let settled = false;
    const finish = (callback) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      channel.port1.close();
      worker.terminate();
      callback();
    };
    const timeout = setTimeout(() => {
      finish(() => reject(new Error('Web Runtime 超时，Worker 已终止。')));
    }, 4_000);
    channel.port1.onmessage = event => {
      try {
        verifyLaunchControl(event.data?.control, request, {
          user_id: control.user_id,
          app_id: control.app_id,
          artifact_digest: control.artifact_digest,
          generation_id: control.generation_id,
          session_id: control.session_id,
          view_revision: control.view_revision,
          event_id: control.event_id,
          channel_id: control.channel_id,
          nonce: control.nonce,
          request_id: control.request_id,
          sequence: 2,
          kind: 'launch-result',
        });
        if (!event.data?.ok) throw new Error(event.data?.error || 'Web Runtime 执行失败。');
        const result = verifyRuntimeResult(request, event.data.result);
        foregroundSession = new ForegroundClient({ port: channel.port1, worker, binding: result.runtime_binding, appId });
        settled = true;
        clearTimeout(timeout);
        foregroundAppId = appId;
        resolve(result);
      } catch (error) {
        finish(() => reject(error instanceof Error ? error : new Error('Web Runtime 返回了未绑定当前会话的结果。')));
      }
    };
    worker.onerror = () => {
      finish(() => reject(new Error('Web Runtime Worker 执行失败。')));
    };
    worker.postMessage({
      schema_version: 'vibapp.preview-port-bind.experimental-v1',
      channel_id: control.channel_id,
      nonce: control.nonce,
      port: channel.port2,
    }, [channel.port2]);
    channel.port1.postMessage({ control, request });
  });
}

async function invoke(command, args = {}) {
  await storageReady;
  await initialize();
  const payload = args.payload || {};
  if (command === 'get_state') {
    const [state, product] = await Promise.all([
      readState(),
      parentProduct('get_state', {}),
    ]);
    return {
      meta: {
        ...(product.meta || {}),
        schema_version: 'vibapp.web-state.experimental.v1',
        runtime_mode: 'browser-wasm',
        ecosystem_role: 'launcher-appstore-runtime',
        web_wasm_sha256: wasm.sha256,
        browser_background_reliability: 'foreground-only',
        local_codeagent_connected: product.meta?.local_codeagent_connected === true,
        installation_performed: false,
      },
      needs: state.needs,
      jobs: Array.isArray(product.jobs) ? product.jobs : [],
      apps: webApps(product.apps, state.installIntents),
      install_intents: state.installIntents,
      feed_errors: Array.isArray(product.feed_errors) ? product.feed_errors : [],
    };
  }
  if (command === 'submit_need') return submitNeed(payload);
  if (command === 'complete_need') return completeNeed(payload);
  if (command === 'get_model_settings') return parentProduct('get_model_settings', {});
  if (command === 'get_codeagent_settings') return parentProduct('get_codeagent_settings', {});
  if (command === 'save_codeagent_settings') return parentProduct('save_codeagent_settings', payload);
  if (command === 'get_network_settings') return parentProduct('get_network_settings', {});
  if (command === 'save_network_settings') return parentProduct('save_network_settings', payload);
  if (command === 'get_network_status') return parentProduct('get_network_status', {});
  if (command === 'start_network_node') return parentProduct('start_network_node', {});
  if (command === 'stop_network_node') return parentProduct('stop_network_node', {});
  if (command === 'create_collaboration_session') return parentProduct('create_collaboration_session', payload);
  if (command === 'join_collaboration_session') return parentProduct('join_collaboration_session', payload);
  if (command === 'leave_collaboration_session') return parentProduct('leave_collaboration_session', payload);
  if (command === 'send_collaboration_event') return parentProduct('send_collaboration_event', payload);
  if (command === 'receive_collaboration_events') return parentProduct('receive_collaboration_events', payload);
  if (command === 'get_collaboration_status') return parentProduct('get_collaboration_status', {});
  if (command === 'save_model_settings') return parentProduct('save_model_settings', payload);
  if (command === 'submit_development_task') return parentProduct('submit_development_task', payload);
  if (command === 'submit_install_intent') {
    if (
      typeof payload.app_id !== 'string'
      || typeof payload.package_digest_sha256 !== 'string'
      || !SHA256.test(payload.package_digest_sha256)
      || Object.keys(payload).sort().join(',') !== 'app_id,package_digest_sha256'
    ) throw new Error('公开应用缓存请求无效。');
    const record = allRecords().find(item => (
      item.app.id === payload.app_id
      && item.package.package_digest_sha256 === payload.package_digest_sha256
      && isPublicRegistryRecord(item)
      && publicLocatorForRecord(item)
    ));
    if (!record) throw new Error('网页仅支持下载具有可验证 P2P 包的公开应用；私有应用请在 VibApp Client 中安装和运行。');
    const rawCache = await parentProduct('ensure_public_app_available', {
      appId: payload.app_id,
      packageDigestSha256: payload.package_digest_sha256,
    });
    if (!exactKeys(rawCache, 'app_id,committed_at_unix_ms,file_count,package_digest_sha256,schema_version,size_bytes,state,web_runtime_available')) {
      throw new Error('网页包缓存回执无效。');
    }
    const cache = rawCache;
    if (
      cache.schema_version !== 'vibapp.website-public-package-cache.experimental-v1'
      || cache.state !== 'cached'
      || cache.app_id !== payload.app_id
      || cache.package_digest_sha256 !== payload.package_digest_sha256
      || !Number.isSafeInteger(cache.file_count)
      || cache.file_count < 2
      || cache.file_count > 128
      || !Number.isSafeInteger(cache.size_bytes)
      || cache.size_bytes < 2
      || cache.size_bytes > 64 * 1024 * 1024
      || !Number.isSafeInteger(cache.committed_at_unix_ms)
      || cache.committed_at_unix_ms < 0
      || typeof cache.web_runtime_available !== 'boolean'
    ) throw new Error('网页包缓存回执与当前应用不一致。');
    const state = await readState();
    const intent = {
      intent_id: 'web-intent-' + Date.now().toString(16),
      app_id: payload.app_id,
      package_digest_sha256: payload.package_digest_sha256,
      status: 'cached-for-web',
      installation_performed: false,
      web_package_cached: true,
      web_runtime_available: cache.web_runtime_available,
      next_authority: cache.web_runtime_available ? 'verified-web-runtime-mirror' : 'vibapp-client',
      cache_receipt: {
        schema_version: cache.schema_version,
        package_digest_sha256: cache.package_digest_sha256,
        file_count: cache.file_count,
        size_bytes: cache.size_bytes,
        committed_at_unix_ms: cache.committed_at_unix_ms,
      },
    };
    state.installIntents.push(intent);
    await writeState(state);
    return { intent };
  }
  if (command === 'launch_app') {
    try {
      return await launchApp(args.appId);
    } catch (error) {
      const reason = classifyInvalidBootstrapError(error);
      if (reason) reportInvalidBootstrap(reason, 'launcher-runtime');
      throw error;
    }
  }
  if (command === 'refresh_app_surface' || command === 'dispatch_app_action') {
    if (!foregroundSession || foregroundAppId !== args.appId) throw new Error('浏览器前台会话已关闭，请重新打开应用。');
    return foregroundSession.call(command === 'refresh_app_surface' ? 'refresh' : 'action', args);
  }
  if (command === 'close_app_window') {
    foregroundSession?.close(); foregroundSession = null; foregroundAppId = null;
    return { closed: true, background_authority_changed: false };
  }
  throw new Error('Web GUI 不支持命令：' + command);
}

async function setLocalePreference(value) {
  if (value !== 'auto' && value !== 'zh-CN' && value !== 'en-US') throw new Error('界面语言设置无效。');
  await storageReady;
  await setParentStorage(LOCALE_KEY, value);
}

window.VibAppWebBridge = { invoke, ready: storageReady, setLocalePreference };
