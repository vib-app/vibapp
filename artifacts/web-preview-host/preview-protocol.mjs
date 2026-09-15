const ORIGIN = /^https?:\/\/[a-z0-9.:[\]-]+(?::[0-9]{1,5})?$/i;
const TOKEN = /^[^\u0000-\u001f\u007f]{16,128}$/u;
const CACHE_VERSION = /^[a-f0-9]{16}$/;
const BIND_KEYS = [
  'channel_id',
  'kind',
  'nonce',
  'preview_origin',
  'schema_version',
].join(',');

function requireValue(condition, detail) {
  if (!condition) throw new Error('preview-bind-rejected:' + detail);
}

export function validateExactOrigin(value, detail = 'origin') {
  requireValue(typeof value === 'string' && ORIGIN.test(value), detail);
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error('preview-bind-rejected:' + detail);
  }
  requireValue(
    parsed.origin === value
      && parsed.username === ''
      && parsed.password === ''
      && parsed.pathname === '/'
      && parsed.search === ''
      && parsed.hash === '',
    detail,
  );
  return parsed.origin;
}

export function validateToken(value, detail) {
  requireValue(typeof value === 'string' && TOKEN.test(value), detail);
  return value;
}

export function validateGuiCacheVersion(value) {
  requireValue(typeof value === 'string' && CACHE_VERSION.test(value), 'gui-manifest-cache-version');
  return value;
}

export function validateGuiSyncManifest(manifest) {
  requireValue(manifest && typeof manifest === 'object' && !Array.isArray(manifest), 'gui-manifest');
  requireValue(manifest.schema_version === 'vibapp.web-gui-sync.experimental-v1', 'gui-manifest-schema');
  requireValue(manifest.adapter && typeof manifest.adapter === 'object' && !Array.isArray(manifest.adapter), 'gui-manifest-adapter');
  validateGuiCacheVersion(manifest.adapter.cache_version);
  requireValue(
    manifest.adapter.bridge === `web-bridge.js?v=${manifest.adapter.cache_version}`,
    'gui-manifest-bridge-version',
  );
  requireValue(manifest.files?.['app.js']?.exact_match === true, 'gui-manifest-app-drift');
  requireValue(
    manifest.files['app.js'].source_sha256 === manifest.files['app.js'].generated_sha256,
    'gui-manifest-app-digest',
  );
  return manifest.adapter.cache_version;
}

export function validateParentBinding(binding, context) {
  requireValue(context?.sourceIsParent === true, 'source');
  requireValue(context?.portCount === 1, 'port-count');
  requireValue(context.eventOrigin === context.expectedParentOrigin, 'parent-origin');
  requireValue(binding && typeof binding === 'object' && !Array.isArray(binding), 'message');
  requireValue(Object.keys(binding).sort().join(',') === BIND_KEYS, 'keys');
  requireValue(binding.schema_version === 'vibapp.preview-frame-bind.experimental-v1', 'schema');
  requireValue(binding.kind === 'bind-preview-frame', 'kind');
  requireValue(binding.preview_origin === context.expectedPreviewOrigin, 'preview-origin');
  requireValue(binding.nonce === context.expectedNonce, 'nonce');
  validateToken(binding.channel_id, 'channel-id');
  validateToken(binding.nonce, 'nonce');
  return binding;
}

export function classifyParentBindingFailure(binding, context) {
  if (context?.eventOrigin !== context?.expectedParentOrigin) return 'wrong-origin';
  if (binding && typeof binding === 'object' && !Array.isArray(binding)) {
    const keys = Object.keys(binding);
    if (keys.some(key => !BIND_KEYS.split(',').includes(key))) return 'extra-authority';
    if (binding.nonce !== context?.expectedNonce) return 'wrong-nonce';
  }
  return 'tamper';
}

export function previewBoundary() {
  return {
    schema_version: 'vibapp.preview-origin-boundary.experimental-v1',
    isolation_state: 'partial',
    stage0_activation_eligible: false,
    completed_controls: [
      'separate-preview-origin',
      'cookie-and-authorization-rejecting-host',
      'no-set-cookie-responses',
      'exact-parent-origin-frame-policy',
      'nonce-bound-single-message-port-parent-to-broker-bootstrap',
      'content-addressed-static-browser-module-responses',
      'no-blob-or-data-executable-module-urls',
      'bounded-privacy-safe-durable-invalid-bootstrap-audit',
    ],
    blockers: [
      'jco-wasm-compilation-still-needs-wasm-unsafe-eval',
      'launcher-and-worker-reverification-still-needs-same-origin-connect',
      'same-origin-nested-launcher-still-combines-allow-scripts-and-allow-same-origin',
      'local-derivation-verifier-is-not-fresh-formal-acceptance',
      'product-web-activation-policy-is-not-accepted-stage0-source',
    ],
  };
}
