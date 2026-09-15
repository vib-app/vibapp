import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const coreRoot = dirname(fileURLToPath(import.meta.url));
const artifactsRoot = dirname(coreRoot);
const publicRoot = join(artifactsRoot, 'product-platform', 'website', 'public');
const candidateRoot = join(
  artifactsRoot,
  'app-builder',
  'demo-output',
  'pipeline',
  'candidates',
  'dfad1fed5fe0eb8c5bf83927eee64695287e22b5c8149d878b231d69d056e674',
);
const deriveProgram = join(coreRoot, 'derive-browser-component.mjs');
const SHA256 = /^[0-9a-f]{64}$/;

function fail(detail) {
  throw new Error('browser-derivation-verifier-failure:' + detail);
}

function requireValue(condition, detail) {
  if (!condition) fail(detail);
}

function canonicalJson(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(canonicalJson).join(',') + ']';
  return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonicalJson(value[key])).join(',') + '}';
}

function sha256(value) {
  return createHash('sha256').update(value).digest('hex');
}

async function readJson(path) {
  return JSON.parse(await readFile(path, 'utf8'));
}

function generatedPath(root, descriptor) {
  requireValue(descriptor.path.startsWith('/launcher/'), 'descriptor-path-prefix');
  return join(root, descriptor.path.slice('/launcher/'.length));
}

function verifyDescriptor(descriptor, componentSha256) {
  requireValue(descriptor && typeof descriptor === 'object', 'descriptor');
  requireValue(descriptor.path.startsWith('/launcher/components/' + componentSha256 + '/'), 'descriptor-component-path');
  requireValue(SHA256.test(descriptor.sha256), 'descriptor-digest');
  requireValue(Number.isInteger(descriptor.size_bytes) && descriptor.size_bytes > 0, 'descriptor-size');
  const name = descriptor.path.split('/').at(-1);
  requireValue(name.includes('-' + descriptor.sha256 + '.'), 'descriptor-content-addressed-name');
}

async function verifyFile(root, descriptor, componentSha256) {
  verifyDescriptor(descriptor, componentSha256);
  const bytes = await readFile(generatedPath(root, descriptor));
  requireValue(bytes.byteLength === descriptor.size_bytes, 'file-size-' + descriptor.path);
  requireValue(sha256(bytes) === descriptor.sha256, 'file-digest-' + descriptor.path);
  return bytes;
}

async function verifyPublishedInput() {
  const [snapshot, candidate, candidateBytes, manifest, manifestBytes, componentBytes] = await Promise.all([
    readJson(join(publicRoot, 'data', 'registry.snapshot.json')),
    readJson(join(candidateRoot, 'candidate.json')),
    readFile(join(candidateRoot, 'candidate.json')),
    readJson(join(candidateRoot, 'package', 'manifest.json')),
    readFile(join(candidateRoot, 'package', 'manifest.json')),
    readFile(join(candidateRoot, 'package', 'component.wasm')),
  ]);
  requireValue(candidate.schema_version === 'vibapp.builder-candidate.experimental-v1', 'candidate-schema');
  requireValue(candidate.document_type === 'verifier-promoted-candidate', 'candidate-document-type');
  requireValue(candidate.state === 'candidate-ready', 'candidate-state');
  requireValue(candidate.verification?.authority === 'independent-verifier', 'canonical-candidate-verifier');
  for (const id of ['artifact-and-package-digests', 'wasm-tools-validate', 'component-import-reconciliation', 'component-tool-metadata']) {
    requireValue(candidate.verification.checks?.some(check => check.id === id && check.outcome === 'pass'), 'canonical-candidate-check-' + id);
  }
  requireValue(sha256(componentBytes) === candidate.component.sha256, 'canonical-component-digest');
  requireValue(componentBytes.byteLength === candidate.component.size_bytes, 'canonical-component-size');
  requireValue(manifest.artifacts?.canonical_component?.sha256 === candidate.component.sha256, 'manifest-component-digest');
  requireValue(manifest.artifacts?.canonical_component?.size_bytes === candidate.component.size_bytes, 'manifest-component-size');
  requireValue(sha256(manifestBytes) === candidate.manifest.sha256, 'manifest-digest');

  requireValue(snapshot.browser_artifact_bindings?.length === 1, 'published-binding-count');
  const binding = snapshot.browser_artifact_bindings[0];
  const previewRecord = snapshot.browser_preview_records?.find(item => item.record_id === binding.preview_record_id);
  requireValue(binding.derived_from_sha256 === candidate.component.sha256, 'binding-derived-from-canonical');
  requireValue(binding.canonical_component.sha256 === candidate.component.sha256, 'binding-canonical-component');
  requireValue(binding.canonical_package_digest_sha256 === candidate.package_digest_sha256, 'binding-canonical-package');
  requireValue(binding.attestation?.stage0_activation_eligible === false, 'binding-stage0-boundary');
  requireValue(previewRecord?.compatibility?.stage0_activation_eligible === false, 'record-stage0-boundary');
  requireValue((manifest.runtime?.profiles || []).every(row => row.profile !== 'web-preview' && row.profile !== 'web-runtime'), 'canonical-manifest-browser-profile-unexpected');
  requireValue((manifest.artifacts?.browser_derivations || []).length === 0, 'canonical-manifest-browser-derivation-unexpected');
  requireValue(Array.isArray(binding.files) && binding.files.length === 2, 'binding-file-count');
  requireValue(canonicalJson(binding.files.find(item => item.path === binding.entry.path)) === canonicalJson(binding.entry), 'binding-entry-selector');
  requireValue(canonicalJson(binding.files.find(item => item.path === binding.host_adapter.path)) === canonicalJson(binding.host_adapter), 'binding-adapter-selector');
  requireValue(binding.host_adapter_sha256 === binding.host_adapter.sha256, 'binding-adapter-digest');

  const publishedFiles = new Map();
  for (const descriptor of binding.files) {
    publishedFiles.set(descriptor.path, await verifyFile(join(publicRoot, 'launcher'), descriptor, candidate.component.sha256));
  }
  const entryText = publishedFiles.get(binding.entry.path).toString('utf8');
  requireValue(!entryText.includes('data:') && !entryText.includes('blob:'), 'entry-url-module-import');
  requireValue(entryText.includes("from './" + binding.host_adapter.path.split('/').at(-1) + "'"), 'entry-static-adapter-import');
  const adapterText = publishedFiles.get(binding.host_adapter.path).toString('utf8');
  requireValue(!adapterText.includes('data:') && !adapterText.includes('blob:'), 'adapter-url-module-import');

  const attestationBytes = await verifyFile(join(publicRoot, 'launcher'), binding.attestation.artifact, candidate.component.sha256);
  const attestation = JSON.parse(attestationBytes.toString('utf8'));
  requireValue(attestationBytes.toString('utf8') === canonicalJson(attestation) + '\n', 'attestation-canonical-json');
  requireValue(attestation.canonical_input?.candidate_document_sha256 === sha256(candidateBytes), 'attestation-candidate-document');
  requireValue(attestation.derivation?.derived_from_sha256 === candidate.component.sha256, 'attestation-derived-from');
  requireValue(canonicalJson(attestation.derivation?.output_inventory) === canonicalJson(binding.files), 'attestation-output-inventory');
  requireValue(attestation.authority?.independent_verifier === 'not-run-for-browser-derivation', 'attestation-formal-verifier-boundary');
  requireValue(attestation.authority?.stage0_activation_eligible === false, 'attestation-stage0-boundary');
  return { attestation, binding, candidate, previewRecord };
}

export async function verifyBrowserDerivation() {
  const published = await verifyPublishedInput();
  const rederiveRoot = await mkdtemp(join(tmpdir(), 'vibapp-browser-verifier-'));
  try {
    const child = spawnSync(process.execPath, [deriveProgram, rederiveRoot], {
      cwd: coreRoot,
      env: { ...process.env, npm_config_offline: 'true' },
      encoding: 'utf8',
      maxBuffer: 8 * 1024 * 1024,
    });
    requireValue(child.status === 0, 'rederivation-process:' + (child.stderr || child.stdout).trim());
    const rederived = JSON.parse(child.stdout);
    requireValue(canonicalJson(rederived.binding) === canonicalJson(published.binding), 'rederived-binding-drift');
    requireValue(canonicalJson(rederived.preview_record) === canonicalJson(published.previewRecord), 'rederived-preview-record-drift');
    requireValue(canonicalJson(rederived.attestation_document) === canonicalJson(published.attestation), 'rederived-attestation-drift');
    for (const descriptor of published.binding.files) {
      const [publishedBytes, rederivedBytes] = await Promise.all([
        readFile(generatedPath(join(publicRoot, 'launcher'), descriptor)),
        verifyFile(rederiveRoot, descriptor, published.candidate.component.sha256),
      ]);
      requireValue(publishedBytes.equals(rederivedBytes), 'rederived-file-drift-' + descriptor.path);
    }
    const rederivedAttestation = await verifyFile(rederiveRoot, published.binding.attestation.artifact, published.candidate.component.sha256);
    const publishedAttestation = await readFile(generatedPath(join(publicRoot, 'launcher'), published.binding.attestation.artifact));
    requireValue(publishedAttestation.equals(rederivedAttestation), 'rederived-attestation-file-drift');

    const entryPath = generatedPath(rederiveRoot, published.binding.entry);
    const component = await import(pathToFileURL(entryPath).href + '?local-independent-verifier=1');
    requireValue(typeof component.guest?.describe === 'function' && typeof component.guest?.handleEvent === 'function', 'rederived-guest-exports');
    const descriptor = component.guest.describe();
    requireValue(descriptor.id === published.binding.app_id, 'rederived-guest-app');

    return {
      schema_version: 'vibapp.browser-derivation-local-verifier.experimental-v1',
      state: 'local-independent-browser-derivation-verifier-pass',
      verifier_process: 'separate-node-child-rederivation',
      canonical_package_digest_sha256: published.binding.canonical_package_digest_sha256,
      canonical_component_sha256: published.binding.canonical_component.sha256,
      binding_payload_sha256: published.binding.attestation.binding_payload_sha256,
      entry_sha256: published.binding.entry.sha256,
      file_count: published.binding.files.length,
      checks: [
        'canonical-candidate-rehashed',
        'canonical-manifest-browser-ineligibility-confirmed',
        'binding-and-attestation-reconciled',
        'all-derived-files-rehashed',
        'fresh-process-rederivation-byte-identical',
        'rederived-guest-exports-executed',
        'blob-and-data-module-imports-absent',
      ],
      formal_stage0_acceptance: 'not-claimed',
      stage0_activation_eligible: false,
      remaining_formal_blockers: [
        'canonical-manifest-declares-no-browser-profile-or-browser-derivation',
        'local-verifier-run-is-not-a-fresh-formal-acceptance-owner',
        'jco-toolchain-and-browser-execution-policy-are-not-stage0-accepted',
      ],
    };
  } finally {
    await rm(rederiveRoot, { recursive: true, force: true });
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.stdout.write(canonicalJson(await verifyBrowserDerivation()) + '\n');
}
