import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import {
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  realpath,
  rm,
  writeFile,
} from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { basename, dirname, isAbsolute, join, relative, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const coreRoot = dirname(fileURLToPath(import.meta.url));
const artifactsRoot = dirname(coreRoot);
const repoRoot = dirname(artifactsRoot);
const candidateRoot = join(
  artifactsRoot,
  'app-builder',
  'demo-output',
  'pipeline',
  'candidates',
  'dfad1fed5fe0eb8c5bf83927eee64695287e22b5c8149d878b231d69d056e674',
);
const candidatePath = join(candidateRoot, 'candidate.json');
const componentPath = join(candidateRoot, 'package', 'component.wasm');
const manifestPath = join(candidateRoot, 'package', 'manifest.json');

const EXPECTED = Object.freeze({
  appId: 'ai.vibapp.hello',
  componentSha256: 'd3844f16cdfee3634a3652d6f5e43d54adad18c198cf3b6837a6d5d149e661aa',
  componentSize: 46_761,
  packageSha256: 'dfad1fed5fe0eb8c5bf83927eee64695287e22b5c8149d878b231d69d056e674',
  jcoVersion: '1.15.4',
});

const HOST_ADAPTER = Object.freeze({
  schema_version: 'vibapp.web-host-adapter.experimental-v1',
  profile: 'web-preview',
  background_reliability: 'foreground-only',
  ambient_wasi_linked: false,
  install_authority: false,
  network_access: 'none',
  persistent_storage: 'none',
  imports: [
    { interface: 'vibapp:experimental-v0/clock@0.0.1', availability: 'brokered', behavior: 'worker monotonic clock only' },
    { interface: 'vibapp:experimental-v0/host-info@0.0.1', availability: 'brokered', behavior: 'fixed foreground-only description' },
    { interface: 'vibapp:experimental-v0/kv@0.0.1', availability: 'mock', behavior: 'empty read-only view' },
    { interface: 'vibapp:experimental-v0/log@0.0.1', availability: 'mock', behavior: 'bounded no-op sink' },
    { interface: 'vibapp:experimental-v0/settings@0.0.1', availability: 'mock', behavior: 'empty revision-one snapshot' },
  ],
});

const HOST_ADAPTER_MODULE = Buffer.from(`export function current() {
  return { schemaRevision: 1n, configRevision: 1n, values: [] };
}

export function describeHost() {
  return { profile: 'web-preview', background: 'foreground-only', capabilities: [] };
}

export function get() {
  return undefined;
}

export function monotonicNow() {
  return BigInt(Math.floor(performance.now()));
}

export function write() {}
`, 'utf8');

const HOST_IMPORTS = Object.freeze([
  'vibapp:experimental-v0/clock',
  'vibapp:experimental-v0/host-info',
  'vibapp:experimental-v0/kv',
  'vibapp:experimental-v0/log',
  'vibapp:experimental-v0/settings',
]);

function fail(detail) {
  throw new Error('browser-derivation-failure:' + detail);
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

function exactJsonBytes(value) {
  return Buffer.from(canonicalJson(value) + '\n', 'utf8');
}

function bindingPayload(binding) {
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
      kind: binding.attestation.kind,
      trusted_builder_policy: binding.attestation.trusted_builder_policy,
      verification_state: binding.attestation.verification_state,
      canonical_component_transformation_proven: binding.attestation.canonical_component_transformation_proven,
      stage0_activation_eligible: binding.attestation.stage0_activation_eligible,
    },
  };
}

async function readJson(path) {
  return JSON.parse(await readFile(path, 'utf8'));
}

function checkedSpawn(command, args, detail, options = {}) {
  const result = spawnSync(command, args, {
    cwd: repoRoot,
    env: { ...process.env, npm_config_offline: 'true' },
    encoding: 'utf8',
    maxBuffer: 4 * 1024 * 1024,
    ...options,
  });
  if (result.status !== 0) {
    process.stderr.write(result.stderr || result.stdout || detail + '\n');
    fail(detail);
  }
  return result.stdout.trim();
}

async function resolveJco() {
  if (process.env.VIBAPP_JCO_BIN) {
    requireValue(isAbsolute(process.env.VIBAPP_JCO_BIN), 'jco-path-not-absolute');
    return realpath(process.env.VIBAPP_JCO_BIN);
  }
  const npm = process.env.VIBAPP_NPM_BIN || (process.platform === 'win32' ? 'npm.cmd' : 'npm');
  if (process.env.VIBAPP_NPM_BIN) requireValue(isAbsolute(npm), 'npm-path-not-absolute');
  const located = checkedSpawn(npm, [
    'exec', '--offline', '--yes=false',
    '--package=@bytecodealliance/jco@' + EXPECTED.jcoVersion,
    '--', 'which', 'jco',
  ], 'jco-offline-cache-unavailable');
  requireValue(isAbsolute(located), 'jco-resolution-not-absolute');
  return realpath(located);
}

async function inspectCandidate() {
  const [candidateBytes, candidate, componentBytes, manifestBytes, manifest] = await Promise.all([
    readFile(candidatePath),
    readJson(candidatePath),
    readFile(componentPath),
    readFile(manifestPath),
    readJson(manifestPath),
  ]);
  requireValue(candidate.schema_version === 'vibapp.builder-candidate.experimental-v1', 'candidate-schema');
  requireValue(candidate.document_type === 'verifier-promoted-candidate', 'candidate-document-type');
  requireValue(candidate.state === 'candidate-ready', 'candidate-state');
  requireValue(candidate.verification?.authority === 'independent-verifier', 'candidate-verifier-authority');
  for (const id of ['artifact-and-package-digests', 'wasm-tools-validate', 'component-import-reconciliation', 'component-tool-metadata']) {
    requireValue(candidate.verification.checks?.some(check => check.id === id && check.outcome === 'pass'), 'candidate-check-' + id);
  }
  requireValue(candidate.package_digest_sha256 === EXPECTED.packageSha256, 'candidate-package-digest');
  requireValue(candidate.component?.sha256 === EXPECTED.componentSha256, 'candidate-component-descriptor-digest');
  requireValue(candidate.component?.size_bytes === EXPECTED.componentSize, 'candidate-component-descriptor-size');
  requireValue(componentBytes.byteLength === EXPECTED.componentSize, 'component-size');
  requireValue(sha256(componentBytes) === EXPECTED.componentSha256, 'component-digest');
  requireValue(componentBytes.subarray(0, 8).equals(Buffer.from([0, 97, 115, 109, 13, 0, 1, 0])), 'component-model-header');
  requireValue(sha256(manifestBytes) === candidate.manifest?.sha256, 'manifest-digest');
  requireValue(manifest.app?.id === EXPECTED.appId, 'manifest-app-id');
  requireValue(manifest.artifacts?.canonical_component?.sha256 === EXPECTED.componentSha256, 'manifest-component-digest');
  requireValue(manifest.artifacts?.canonical_component?.size_bytes === EXPECTED.componentSize, 'manifest-component-size');
  return { candidateBytes, candidate, componentBytes, manifest, manifestBytes };
}

export async function deriveBrowserComponent(outputRoot) {
  requireValue(isAbsolute(outputRoot), 'output-root-not-absolute');
  const { candidateBytes, candidate, manifest } = await inspectCandidate();
  const jco = await resolveJco();
  const jcoVersion = checkedSpawn(jco, ['--version'], 'jco-version');
  requireValue(jcoVersion === EXPECTED.jcoVersion, 'jco-version-mismatch');
  const jcoPackageRoot = dirname(dirname(jco));
  const jcoPackageJsonPath = join(jcoPackageRoot, 'package.json');
  const [jcoEntryBytes, jcoPackageBytes] = await Promise.all([readFile(jco), readFile(jcoPackageJsonPath)]);
  const jcoPackage = JSON.parse(jcoPackageBytes.toString('utf8'));
  requireValue(jcoPackage.name === '@bytecodealliance/jco' && jcoPackage.version === EXPECTED.jcoVersion, 'jco-package-identity');

  const workRoot = await mkdtemp(join(tmpdir(), 'vibapp-jco-'));
  const outRoot = join(workRoot, 'out');
  const hostAdapterSha256 = sha256(HOST_ADAPTER_MODULE);
  const hostAdapterName = 'host-adapter-' + hostAdapterSha256 + '.mjs';
  const mappingArgs = HOST_IMPORTS.flatMap(specifier => ['-M', specifier + '=./' + hostAdapterName]);
  const commandArgs = [
    'transpile', componentPath,
    '-o', outRoot,
    '--name', 'hello',
    '--no-typescript',
    '--no-wasi-shim',
    '--base64-cutoff=1000000',
    '-q',
    ...mappingArgs,
  ];
  let artifactBytes;
  try {
    checkedSpawn(jco, commandArgs, 'jco-transpile');
    const inventory = (await readdir(outRoot)).sort();
    requireValue(inventory.length === 1 && inventory[0] === 'hello.js', 'jco-output-inventory');
    artifactBytes = await readFile(join(outRoot, 'hello.js'));
  } finally {
    await rm(workRoot, { recursive: true, force: true });
  }
  requireValue(artifactBytes.byteLength > 0 && artifactBytes.byteLength <= 4 * 1024 * 1024, 'derived-artifact-size');
  const artifactText = artifactBytes.toString('utf8');
  requireValue(!artifactText.includes(" from 'vibapp:"), 'unmapped-component-import');
  requireValue(!artifactText.includes('data:') && !artifactText.includes('blob:'), 'url-backed-module-import');
  requireValue(artifactText.includes("from './" + hostAdapterName + "'"), 'static-host-adapter-import-missing');
  requireValue(artifactText.includes("export { guest001 as guest"), 'guest-export-missing');

  const artifactSha256 = sha256(artifactBytes);
  const componentDirectory = join(outputRoot, 'components', EXPECTED.componentSha256);
  await mkdir(componentDirectory, { recursive: true });
  const artifactName = 'hello-' + artifactSha256 + '.jco.mjs';
  const artifactPath = '/launcher/components/' + EXPECTED.componentSha256 + '/' + artifactName;
  const hostAdapterPath = '/launcher/components/' + EXPECTED.componentSha256 + '/' + hostAdapterName;
  await writeFile(join(componentDirectory, artifactName), artifactBytes);
  await writeFile(join(componentDirectory, hostAdapterName), HOST_ADAPTER_MODULE);

  const entry = {
    path: artifactPath,
    media_type: 'text/javascript',
    format: 'jco-esm',
    sha256: artifactSha256,
    size_bytes: artifactBytes.byteLength,
  };
  const hostAdapter = {
    path: hostAdapterPath,
    media_type: 'text/javascript',
    format: 'host-adapter-esm',
    sha256: hostAdapterSha256,
    size_bytes: HOST_ADAPTER_MODULE.byteLength,
  };
  const files = [entry, hostAdapter].sort((left, right) => Buffer.from(left.path).compare(Buffer.from(right.path)));

  const manifestProfiles = (manifest.runtime?.profiles || []).map(item => item.profile).sort();
  const declaredBrowserDerivations = manifest.artifacts?.browser_derivations || [];
  const stage0ActivationEligible = manifestProfiles.some(profile => profile === 'web-preview' || profile === 'web-runtime')
    && declaredBrowserDerivations.some(item => item.format === 'jco-esm' && item.derived_from_sha256 === EXPECTED.componentSha256);
  const previewRecordId = 'browser.preview.' + EXPECTED.appId + '.' + EXPECTED.componentSha256.slice(0, 16);
  const binding = {
    schema_version: 'vibapp.browser-derivation-binding.experimental-v1',
    source_kind: 'verifier-promoted-candidate',
    app_id: EXPECTED.appId,
    profile: 'web-preview',
    preview_record_id: previewRecordId,
    preview_record_revision: 1,
    canonical_package_digest_sha256: EXPECTED.packageSha256,
    canonical_component: {
      media_type: 'application/wasm',
      sha256: EXPECTED.componentSha256,
      size_bytes: EXPECTED.componentSize,
    },
    derived_from_sha256: EXPECTED.componentSha256,
    entry,
    files,
    host_adapter: hostAdapter,
    host_adapter_sha256: hostAdapterSha256,
    attestation: {
      kind: 'local-deterministic-jco-derivation',
      trusted_builder_policy: 'product-platform-exact-jco-1.15.4',
      verification_state: 'locally-derived-awaiting-independent-verifier',
      canonical_component_transformation_proven: true,
      stage0_activation_eligible: stage0ActivationEligible,
    },
  };
  const bindingPayloadSha256 = sha256(canonicalJson(bindingPayload(binding)));
  const attestation = {
    schema_version: 'vibapp.browser-derivation-attestation.experimental-v1',
    document_type: 'local-browser-derivation-attestation',
    state: 'locally-derived-awaiting-independent-verifier',
    binding_payload_sha256: bindingPayloadSha256,
    authority: {
      derivation: 'product-platform-local',
      independent_verifier: 'not-run-for-browser-derivation',
      install: 'none',
      publish: 'none',
      stage0_activation_eligible: stage0ActivationEligible,
    },
    canonical_input: {
      app_id: EXPECTED.appId,
      package_digest_sha256: EXPECTED.packageSha256,
      component_sha256: EXPECTED.componentSha256,
      component_size_bytes: EXPECTED.componentSize,
      candidate_document_sha256: sha256(candidateBytes),
      candidate_state: candidate.state,
      candidate_verifier_authority: candidate.verification.authority,
      candidate_verification_checks: candidate.verification.checks.map(check => ({ id: check.id, outcome: check.outcome, tool: check.tool })),
    },
    derivation: {
      format: 'jco-esm',
      derived_from_sha256: EXPECTED.componentSha256,
      tool: {
        package: '@bytecodealliance/jco',
        version: EXPECTED.jcoVersion,
        resolution: 'npm-cache-offline',
        entry_sha256: sha256(jcoEntryBytes),
        package_json_sha256: sha256(jcoPackageBytes),
      },
      command: [
        'jco', 'transpile', relative(repoRoot, componentPath),
        '-o', '<isolated-output>', '--name', 'hello', '--no-typescript', '--no-wasi-shim',
        '--base64-cutoff=1000000', '-q',
        ...HOST_IMPORTS.flatMap(specifier => ['-M', specifier + '=./' + hostAdapterName]),
      ],
      output_inventory: files,
      host_adapter: HOST_ADAPTER,
      host_adapter_sha256: hostAdapterSha256,
    },
    profile_boundary: {
      requested_preview_profile: 'web-preview',
      canonical_manifest_profiles: manifestProfiles,
      canonical_manifest_browser_derivations: declaredBrowserDerivations,
      stage0_activation_eligible: stage0ActivationEligible,
      blocker: stage0ActivationEligible ? null : 'canonical-manifest-does-not-declare-a-verifier-promoted-browser-profile-and-jco-esm-derivation',
    },
  };
  const attestationBytes = exactJsonBytes(attestation);
  const attestationName = 'attestation-' + sha256(attestationBytes) + '.json';
  const attestationPath = '/launcher/components/' + EXPECTED.componentSha256 + '/' + attestationName;
  await writeFile(join(componentDirectory, attestationName), attestationBytes);
  binding.attestation = {
    ...binding.attestation,
    binding_payload_sha256: bindingPayloadSha256,
    artifact: {
      path: attestationPath,
      media_type: 'application/json',
      sha256: sha256(attestationBytes),
      size_bytes: attestationBytes.byteLength,
    },
  };

  const availability = new Map(HOST_ADAPTER.imports.map(item => [item.interface, item]));
  const previewRecord = {
    schema_version: 'vibapp.browser-preview-record.experimental-v1',
    document_type: 'browser-preview-record-projection',
    record_id: previewRecordId,
    record_revision: 1,
    app: {
      id: manifest.app.id,
      version: manifest.app.version,
      kind: manifest.app.kind,
      display_name: manifest.app.display_name,
      summary: manifest.app.description,
      publisher: { display_name: manifest.app.publisher.display_name, publisher_id: manifest.app.publisher.id, verification_state: 'local-candidate' },
    },
    package: {
      app_id: manifest.app.id,
      version: manifest.app.version,
      package_digest_sha256: EXPECTED.packageSha256,
      manifest_digest_sha256: candidate.manifest.sha256,
    },
    contract: {
      package_format: manifest.package_format,
      component_contract: manifest.runtime.contract,
      wasi: manifest.runtime.wasi,
      wit_world: manifest.runtime.world,
    },
    compatibility: {
      profiles: ['web-preview'],
      platforms: [{ os: 'browser', arch: 'wasm32', profile: 'web-preview' }],
      activation_authority: 'product-platform-preview-adapter',
      canonical_manifest_profiles: manifestProfiles,
      stage0_activation_eligible: stage0ActivationEligible,
    },
    permissions: manifest.capabilities.map(capability => ({
      interface: capability.interface,
      necessity: capability.necessity,
      availability: availability.get(capability.interface)?.availability || 'unavailable',
      summary: availability.get(capability.interface)?.behavior || 'not provided by browser adapter',
    })),
    verification: {
      status: 'locally-derived-awaiting-independent-verifier',
      summary: 'Canonical Component bytes were independently promoted; the Jco browser derivation is local product implementation evidence only.',
    },
    publication: { state: 'private-candidate', published_at_utc: null, revoked_at_utc: null },
    search_metadata: { tags: ['hello', 'preview', 'component'], search_text_digest_sha256: sha256(manifest.app.display_name + ':' + manifest.app.description) },
    source: {
      candidate_path: relative(repoRoot, candidatePath),
      candidate_document_sha256: sha256(candidateBytes),
    },
  };

  return { attestation, binding, previewRecord };
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const outputRoot = process.argv[2] ? resolve(process.argv[2]) : null;
  requireValue(outputRoot, 'output-root-required');
  const result = await deriveBrowserComponent(outputRoot);
  process.stdout.write(canonicalJson({
    app_id: result.binding.app_id,
    entry: result.binding.entry,
    files: result.binding.files,
    attestation: result.binding.attestation.artifact,
    stage0_activation_eligible: result.binding.attestation.stage0_activation_eligible,
    binding: result.binding,
    preview_record: result.previewRecord,
    attestation_document: result.attestation,
  }) + '\n');
}
