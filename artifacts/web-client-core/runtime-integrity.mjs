const SHA256 = /^[0-9a-f]{64}$/;
const SESSION = /^[^\u0000-\u001f\u007f]{16,128}$/u;
const APP_ID = /^[a-z0-9]+(?:[.-][a-z0-9]+)+$/;
const CONTROL_KINDS = new Set(['launch', 'launch-result']);
const CONTROL_KEYS = [
  'app_id',
  'artifact_digest',
  'channel_id',
  'event_id',
  'generation_id',
  'kind',
  'nonce',
  'request_id',
  'schema_version',
  'sequence',
  'session_id',
  'user_id',
  'view_revision',
].join(',');

function fail(detail) {
  throw new Error('integrity-failure:' + detail);
}

function requireValue(condition, detail) {
  if (!condition) fail(detail);
}

export function canonicalJson(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(canonicalJson).join(',') + ']';
  return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonicalJson(value[key])).join(',') + '}';
}

function bytesView(value) {
  if (value instanceof ArrayBuffer) return new Uint8Array(value);
  if (ArrayBuffer.isView(value)) return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  fail('artifact-bytes-type');
}

export async function sha256Hex(value) {
  const bytes = typeof value === 'string' ? new TextEncoder().encode(value) : bytesView(value);
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
}

export function bindingPayload(binding) {
  return {
    schema_version: binding.schema_version,
    source_kind: binding.source_kind,
    app_id: binding.app_id,
    profile: binding.profile,
    preview_record_id: binding.preview_record_id,
    preview_record_revision: binding.preview_record_revision,
    canonical_package_digest_sha256: binding.canonical_package_digest_sha256,
    canonical_component: binding.canonical_component,
    derived_from_sha256: binding.derived_from_sha256,
    entry: binding.entry,
    files: binding.files,
    host_adapter: binding.host_adapter,
    host_adapter_sha256: binding.host_adapter_sha256,
    attestation: {
      kind: binding.attestation?.kind,
      trusted_builder_policy: binding.attestation?.trusted_builder_policy,
      verification_state: binding.attestation?.verification_state,
      canonical_component_transformation_proven: binding.attestation?.canonical_component_transformation_proven,
      stage0_activation_eligible: binding.attestation?.stage0_activation_eligible,
    },
  };
}

function verifyPath(path, componentSha256, suffix) {
  requireValue(typeof path === 'string' && path.startsWith('/launcher/components/' + componentSha256 + '/'), suffix + '-path');
  requireValue(!path.split('/').some((part, index) => index > 0 && (part === '' || part === '.' || part === '..')), suffix + '-path-segment');
}

function verifyDescriptor(descriptor, { componentSha256, mediaType, format, maximumBytes, suffix }) {
  verifyPath(descriptor?.path, componentSha256, suffix);
  requireValue(descriptor.media_type === mediaType, suffix + '-media-type');
  if (format) requireValue(descriptor.format === format, suffix + '-format');
  requireValue(SHA256.test(descriptor.sha256), suffix + '-digest');
  requireValue(Number.isInteger(descriptor.size_bytes) && descriptor.size_bytes > 0 && descriptor.size_bytes <= maximumBytes, suffix + '-size');
}

export async function verifyBrowserDerivationBinding(binding) {
  requireValue(binding?.schema_version === 'vibapp.browser-derivation-binding.experimental-v1', 'binding-schema');
  requireValue(binding.source_kind === 'verifier-promoted-candidate', 'binding-source-kind');
  requireValue(APP_ID.test(binding.app_id || ''), 'binding-app');
  requireValue(binding.profile === 'web-preview', 'binding-profile');
  requireValue(typeof binding.preview_record_id === 'string' && binding.preview_record_id.length > 0, 'binding-record');
  requireValue(Number.isInteger(binding.preview_record_revision) && binding.preview_record_revision > 0, 'binding-revision');
  requireValue(SHA256.test(binding.canonical_package_digest_sha256), 'binding-package-digest');
  requireValue(binding.canonical_component?.media_type === 'application/wasm', 'binding-component-media-type');
  requireValue(SHA256.test(binding.canonical_component?.sha256), 'binding-component-digest');
  requireValue(Number.isInteger(binding.canonical_component?.size_bytes) && binding.canonical_component.size_bytes > 8, 'binding-component-size');
  requireValue(binding.derived_from_sha256 === binding.canonical_component.sha256, 'binding-derived-from');
  requireValue(SHA256.test(binding.host_adapter_sha256), 'binding-host-adapter');
  verifyDescriptor(binding.entry, {
    componentSha256: binding.canonical_component.sha256,
    mediaType: 'text/javascript',
    format: 'jco-esm',
    maximumBytes: 4 * 1024 * 1024,
    suffix: 'entry',
  });
  requireValue(Array.isArray(binding.files) && binding.files.length === 2, 'derivation-file-count');
  const filePaths = new Set();
  for (const [index, descriptor] of binding.files.entries()) {
    verifyDescriptor(descriptor, {
      componentSha256: binding.canonical_component.sha256,
      mediaType: 'text/javascript',
      format: descriptor?.path === binding.entry.path ? 'jco-esm' : 'host-adapter-esm',
      maximumBytes: 4 * 1024 * 1024,
      suffix: 'derivation-file-' + index,
    });
    requireValue(!filePaths.has(descriptor.path), 'derivation-file-path-duplicate');
    filePaths.add(descriptor.path);
  }
  requireValue(binding.files.map(item => item.path).join(',') === [...binding.files].sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0).map(item => item.path).join(','), 'derivation-file-order');
  const selectedEntry = binding.files.find(item => item.path === binding.entry.path);
  requireValue(canonicalJson(selectedEntry) === canonicalJson(binding.entry), 'derivation-entry-selector');
  const selectedAdapter = binding.files.find(item => item.path === binding.host_adapter?.path);
  requireValue(canonicalJson(selectedAdapter) === canonicalJson(binding.host_adapter), 'derivation-host-adapter-selector');
  requireValue(binding.host_adapter.sha256 === binding.host_adapter_sha256, 'binding-host-adapter-file-digest');
  requireValue(binding.attestation?.kind === 'local-deterministic-jco-derivation', 'attestation-kind');
  requireValue(binding.attestation?.trusted_builder_policy === 'product-platform-exact-jco-1.15.4', 'attestation-policy');
  requireValue(binding.attestation?.verification_state === 'locally-derived-awaiting-independent-verifier', 'attestation-state');
  requireValue(binding.attestation?.canonical_component_transformation_proven === true, 'attestation-transformation');
  requireValue(binding.attestation?.stage0_activation_eligible === false, 'attestation-stage0-boundary');
  requireValue(SHA256.test(binding.attestation?.binding_payload_sha256), 'attestation-binding-digest');
  verifyDescriptor(binding.attestation?.artifact, {
    componentSha256: binding.canonical_component.sha256,
    mediaType: 'application/json',
    maximumBytes: 256 * 1024,
    suffix: 'attestation-artifact',
  });
  const actual = await sha256Hex(canonicalJson(bindingPayload(binding)));
  requireValue(actual === binding.attestation.binding_payload_sha256, 'attestation-binding-payload');
  return binding;
}

export async function verifyBrowserPreviewBinding(record, binding) {
  await verifyBrowserDerivationBinding(binding);
  requireValue(record?.schema_version === 'vibapp.browser-preview-record.experimental-v1', 'record-schema');
  requireValue(record?.document_type === 'browser-preview-record-projection', 'record-document-type');
  requireValue(record?.app?.id === binding.app_id, 'record-app');
  requireValue(record?.record_id === binding.preview_record_id, 'record-id');
  requireValue(record?.record_revision === binding.preview_record_revision, 'record-revision');
  requireValue(record?.package?.package_digest_sha256 === binding.canonical_package_digest_sha256, 'record-package-digest');
  requireValue(record?.compatibility?.profiles?.includes(binding.profile), 'record-profile');
  requireValue(record?.compatibility?.activation_authority === 'product-platform-preview-adapter', 'record-activation-authority');
  requireValue(record?.compatibility?.stage0_activation_eligible === false, 'record-stage0-boundary');
  requireValue(record?.verification?.status === binding.attestation.verification_state, 'record-verification-state');
  return binding;
}

export async function verifyArtifactBytes(descriptor, value, suffix = 'artifact') {
  const bytes = bytesView(value);
  requireValue(bytes.byteLength === descriptor?.size_bytes, suffix + '-size-mismatch');
  requireValue(await sha256Hex(bytes) === descriptor?.sha256, suffix + '-digest-mismatch');
  return bytes;
}

export async function verifyDerivationAttestation(binding, value) {
  await verifyBrowserDerivationBinding(binding);
  const bytes = await verifyArtifactBytes(binding.attestation.artifact, value, 'attestation-artifact');
  let document;
  try {
    const text = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
    document = JSON.parse(text);
    requireValue(text === canonicalJson(document) + '\n', 'attestation-noncanonical-json');
  } catch (error) {
    if (error instanceof Error && error.message.startsWith('integrity-failure:')) throw error;
    fail('attestation-json');
  }
  requireValue(document.schema_version === 'vibapp.browser-derivation-attestation.experimental-v1', 'attestation-document-schema');
  requireValue(document.document_type === 'local-browser-derivation-attestation', 'attestation-document-type');
  requireValue(document.state === binding.attestation.verification_state, 'attestation-document-state');
  requireValue(document.binding_payload_sha256 === binding.attestation.binding_payload_sha256, 'attestation-document-binding');
  requireValue(document.authority?.independent_verifier === 'not-run-for-browser-derivation', 'attestation-independent-boundary');
  requireValue(document.authority?.stage0_activation_eligible === false, 'attestation-authority-boundary');
  requireValue(document.canonical_input?.app_id === binding.app_id, 'attestation-app');
  requireValue(document.canonical_input?.package_digest_sha256 === binding.canonical_package_digest_sha256, 'attestation-package');
  requireValue(document.canonical_input?.component_sha256 === binding.canonical_component.sha256, 'attestation-component');
  requireValue(document.canonical_input?.component_size_bytes === binding.canonical_component.size_bytes, 'attestation-component-size');
  requireValue(document.canonical_input?.candidate_state === 'candidate-ready', 'attestation-candidate-state');
  requireValue(document.canonical_input?.candidate_verifier_authority === 'independent-verifier', 'attestation-candidate-verifier');
  requireValue(document.derivation?.format === 'jco-esm', 'attestation-format');
  requireValue(document.derivation?.derived_from_sha256 === binding.derived_from_sha256, 'attestation-derived-from');
  requireValue(document.derivation?.tool?.package === '@bytecodealliance/jco', 'attestation-tool');
  requireValue(document.derivation?.tool?.version === '1.15.4', 'attestation-tool-version');
  requireValue(document.derivation?.tool?.resolution === 'npm-cache-offline', 'attestation-tool-resolution');
  requireValue(document.derivation?.host_adapter_sha256 === binding.host_adapter_sha256, 'attestation-host-adapter');
  requireValue(document.derivation?.output_inventory?.length === binding.files.length, 'attestation-output-count');
  requireValue(canonicalJson(document.derivation?.output_inventory) === canonicalJson(binding.files), 'attestation-output-inventory');
  requireValue(document.profile_boundary?.requested_preview_profile === binding.profile, 'attestation-profile');
  requireValue(document.profile_boundary?.stage0_activation_eligible === false, 'attestation-profile-boundary');
  requireValue(typeof document.profile_boundary?.blocker === 'string' && document.profile_boundary.blocker.length > 0, 'attestation-profile-blocker');
  return document;
}

export async function verifyLaunchEnvelope(request) {
  requireValue(request?.schema_version === 'vibapp.web-worker-launch.experimental-v1', 'launch-schema');
  requireValue(SESSION.test(request.session || ''), 'launch-session');
  await verifyBrowserDerivationBinding(request.binding);
  requireValue(request.app_id === request.binding.app_id, 'launch-app');
  requireValue(request.profile === request.binding.profile, 'launch-profile');
  requireValue(request.package_digest_sha256 === request.binding.canonical_package_digest_sha256, 'launch-package-digest');
  requireValue(request.component_sha256 === request.binding.canonical_component.sha256, 'launch-component-digest');
  requireValue(request.browser_artifact_sha256 === request.binding.entry.sha256, 'launch-artifact-digest');
  requireValue(request.entry_path === request.binding.entry.path, 'launch-entry-path');
  return request;
}

export async function verifyDerivationFileSet(binding, values) {
  await verifyBrowserDerivationBinding(binding);
  requireValue(values instanceof Map && values.size === binding.files.length, 'derivation-file-set');
  for (const descriptor of binding.files) {
    requireValue(values.has(descriptor.path), 'derivation-file-missing');
    const bytes = await verifyArtifactBytes(descriptor, values.get(descriptor.path), 'derivation-file');
    if (descriptor.media_type === 'text/javascript') {
      let text;
      try {
        text = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
      } catch {
        fail('derivation-file-utf8');
      }
      requireValue(!text.includes('data:') && !text.includes('blob:'), 'derivation-url-module-import');
    }
  }
  const entryText = new TextDecoder().decode(bytesView(values.get(binding.entry.path)));
  requireValue(entryText.includes("from './" + binding.host_adapter.path.split('/').at(-1) + "'"), 'derivation-static-host-adapter-import');
  return values;
}

export function verifyControlEnvelope(control, expected = {}) {
  requireValue(control?.schema_version === 'vibapp.preview-control.experimental-v1', 'control-schema');
  requireValue(
    Object.keys(control).sort().join(',') === CONTROL_KEYS,
    'control-keys',
  );
  boundedString(control.user_id, 128, 'control-user');
  requireValue(APP_ID.test(control.app_id || ''), 'control-app');
  requireValue(SHA256.test(control.artifact_digest || ''), 'control-artifact-digest');
  requireValue(SESSION.test(control.generation_id || ''), 'control-generation');
  requireValue(SESSION.test(control.session_id || ''), 'control-session');
  requireValue(Number.isInteger(control.view_revision) && control.view_revision > 0, 'control-view-revision');
  requireValue(SESSION.test(control.event_id || ''), 'control-event');
  requireValue(SESSION.test(control.channel_id || ''), 'control-channel');
  requireValue(SESSION.test(control.nonce || ''), 'control-nonce');
  requireValue(SESSION.test(control.request_id || ''), 'control-request');
  requireValue(Number.isInteger(control.sequence) && control.sequence > 0 && control.sequence <= 2, 'control-sequence');
  requireValue(CONTROL_KINDS.has(control.kind), 'control-kind');
  for (const key of CONTROL_KEYS.split(',')) {
    if (expected[key] !== undefined) {
      requireValue(control[key] === expected[key], 'control-' + key.replaceAll('_', '-') + '-mismatch');
    }
  }
  return control;
}

export function verifyLaunchControl(control, request, expected = {}) {
  verifyControlEnvelope(control, expected);
  requireValue(control.app_id === request?.app_id, 'control-launch-app');
  requireValue(control.artifact_digest === request?.browser_artifact_sha256, 'control-launch-artifact');
  requireValue(control.generation_id === 'web-generation-' + request?.component_sha256?.slice(0, 24), 'control-launch-generation');
  requireValue(control.session_id === request?.session, 'control-launch-session');
  return control;
}

function boundedString(value, maximum, detail) {
  requireValue(typeof value === 'string' && value.length > 0 && value.length <= maximum, detail);
}

export function verifyGuestLaunchOutput(request, descriptor, output) {
  requireValue(descriptor && typeof descriptor === 'object', 'guest-descriptor');
  requireValue(descriptor.id === request.app_id, 'guest-descriptor-app');
  boundedString(descriptor.version, 64, 'guest-descriptor-version');
  boundedString(descriptor.displayName, 128, 'guest-descriptor-name');
  requireValue(descriptor.kind === 'ui', 'guest-descriptor-kind');
  requireValue(Array.isArray(descriptor.entrypoints) && descriptor.entrypoints.some(item => item.id === 'main' && item.kind === 'launcher-ui'), 'guest-entrypoint');
  requireValue(output && Array.isArray(output.surfaces) && output.surfaces.length === 1, 'guest-surface-count');
  const surface = output.surfaces[0];
  requireValue(surface.session === request.session, 'guest-surface-session');
  boundedString(surface.surface, 128, 'guest-surface-id');
  boundedString(surface.route, 128, 'guest-surface-route');
  boundedString(surface.view?.title, 256, 'guest-view-title');
  boundedString(surface.view?.root, 128, 'guest-view-root');
  requireValue(Array.isArray(surface.view?.nodes) && surface.view.nodes.length > 0 && surface.view.nodes.length <= 256, 'guest-view-node-count');
  const ids = new Set();
  for (const node of surface.view.nodes) {
    boundedString(node?.id, 128, 'guest-node-id');
    requireValue(!ids.has(node.id), 'guest-node-duplicate');
    ids.add(node.id);
    requireValue(node.parent === undefined || typeof node.parent === 'string', 'guest-node-parent');
    requireValue(node.kind && typeof node.kind === 'object' && typeof node.kind.tag === 'string', 'guest-node-kind');
  }
  requireValue(ids.has(surface.view.root), 'guest-root-missing');
  for (const node of surface.view.nodes) {
    requireValue(node.parent === undefined || ids.has(node.parent), 'guest-node-orphan');
  }
  return { descriptor, surface };
}

export function verifyRuntimeResult(request, result) {
  requireValue(result?.schema_version === 'vibapp.runtime-launch.experimental.v1', 'result-schema');
  requireValue(result?.descriptor?.id === request.app_id, 'result-app');
  requireValue(result?.surface?.session === request.session, 'result-session');
  requireValue(result?.package_digest_sha256 === request.binding.canonical_package_digest_sha256, 'result-package-digest');
  requireValue(result?.component_sha256 === request.binding.canonical_component.sha256, 'result-component-digest');
  requireValue(result?.browser_artifact_sha256 === request.binding.entry.sha256, 'result-artifact-digest');
  requireValue(result?.derivation_binding?.derived_from_sha256 === request.binding.derived_from_sha256, 'result-derived-from');
  requireValue(result?.derivation_binding?.binding_payload_sha256 === request.binding.attestation.binding_payload_sha256, 'result-attestation');
  requireValue(result?.derivation_binding?.verification_state === 'locally-derived-awaiting-independent-verifier', 'result-verification-state');
  requireValue(result?.derivation_binding?.canonical_component_transformation_proven === true, 'result-transformation');
  requireValue(result?.derivation_binding?.stage0_activation_eligible === false, 'result-stage0-boundary');
  const expectedBinding = {
    entrypoint: 'main', package_digest_sha256: request.binding.canonical_package_digest_sha256,
    component_sha256: request.binding.canonical_component.sha256,
    generation: 'web-generation-' + request.binding.canonical_component.sha256.slice(0, 24),
    session: request.session, surface: result.surface.surface, route: result.surface.route,
  };
  requireValue(result.runtime_binding && Object.keys(result.runtime_binding).sort().join(',') === Object.keys(expectedBinding).sort().join(',')
    && Object.entries(expectedBinding).every(([key, value]) => result.runtime_binding[key] === value), 'result-foreground-binding');
  return result;
}
