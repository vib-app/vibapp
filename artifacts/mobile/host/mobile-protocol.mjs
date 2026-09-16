const STORAGE_KEYS = new Set(['vibapp.web-launcher.state.v1', 'vibapp.ui_locale']);
const MAX_BYTES = 1024 * 1024;
const encoder = new TextEncoder();
const exactKeys = (value, keys) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).sort().join(',') === keys;

export function validateStorageRequest(value) {
  return exactKeys(value, 'key,kind,request_id,schema_version,value')
    && value.schema_version === 'vibapp.web-parent-storage.experimental-v1'
    && typeof value.request_id === 'string' && value.request_id.length <= 100
    && STORAGE_KEYS.has(value.key)
    && ((value.kind === 'storage-get' && value.value === null)
      || (value.kind === 'storage-set' && typeof value.value === 'string'
        && encoder.encode(value.value).length <= MAX_BYTES));
}

export function validateProductRequest(value) {
  try { return exactKeys(value, 'args,command,kind,request_id,schema_version')
    && value.schema_version === 'vibapp.web-parent-product.experimental-v1'
    && value.kind === 'product-invoke'
    && typeof value.request_id === 'string' && value.request_id.length <= 100
    && typeof value.command === 'string' && value.command.length <= 80
    && value.args && typeof value.args === 'object' && !Array.isArray(value.args)
    && encoder.encode(JSON.stringify(value.args)).length <= MAX_BYTES;
  } catch { return false; }
}

export function mobileProduct(command) {
  const unavailable = { configured: false, readOnly: true, unavailableReason: 'mobile-remote-service-not-connected' };
  switch (command) {
    case 'get_state': return {
      meta: {
        runtime_mode: 'android-foreground-wasm', ecosystem_role: 'launcher-appstore-runtime',
        cloud_agent_connected: false, local_codeagent_connected: false,
        codeagent_queue_enabled: false, mutation_authority: false,
      },
      apps: [], jobs: [], feed_errors: [],
    };
    case 'get_model_settings': return {
      ...unavailable,
      generation: { enabled: false, baseUrl: '', model: '', protocol: 'responses', timeoutSeconds: 45, maxOutputTokens: 512, temperature: 0, hasApiKey: false },
      embedding: { enabled: false, baseUrl: '', model: '', dimensions: 384, timeoutSeconds: 5, hasApiKey: false },
    };
    case 'get_codeagent_settings': return { ...unavailable, selectedProvider: '', modelByProvider: {}, providers: [] };
    case 'get_network_settings': return {
      ...unavailable, p2pEnabled: false, seedVerifiedApps: false, allowUserFileSeeding: false,
      uploadLimitKibPerSecond: 1024, downloadLimitKibPerSecond: 4096, cacheLimitMib: 128,
      maxActiveTransfers: 8, rtcEnabled: false, maxActiveChannels: 8,
      turnEnabled: false, turnUrls: [], turnUsername: '', hasTurnCredential: false,
    };
    case 'get_network_status': return { state: 'unavailable', foregroundOnly: true, activeTransfers: 0, torrents: 0, lastError: null };
    case 'get_collaboration_status': return { state: 'unavailable', foregroundOnly: true, sessions: [] };
    default: throw new Error('This Android preview runs bundled Web/Wasm apps. Development, background services and network sharing are not connected. / 此 Android 预览版可运行内置 Web/Wasm 应用；开发、后台服务和网络共享尚未连接。');
  }
}
