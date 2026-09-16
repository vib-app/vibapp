import { createHash } from 'node:crypto';
import { copyFile, lstat, mkdir, readFile, realpath, rm, writeFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { dirname, isAbsolute, join, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { deriveBrowserComponent } from './derive-browser-component.mjs';
import { cargoPath } from '../../scripts/toolchains.mjs';
import {
  isPublicRegistryRecord,
  isSyntheticRegistryRecord,
  isRealPublicBrowserBinding,
  syncPublicRegistryDataRoot,
  verifyPublicRegistryDataRoot,
} from './sync-public-package-locators.mjs';

const coreRoot = dirname(fileURLToPath(import.meta.url));
const artifactsRoot = dirname(coreRoot);
const desktopUi = join(artifactsRoot, 'desktop', 'ui');
const websiteRoot = join(artifactsRoot, 'product-platform', 'website');
const registryRoot = join(artifactsRoot, 'product-platform', 'registry');
const registrySource = join(registryRoot, 'snapshots', 'registry.snapshot.json');
const stableProductionPublicDataRoot = join(registryRoot, 'generated', 'production-public-data');
const launcherRoot = join(websiteRoot, 'public', 'launcher');
const targetRoot = join(coreRoot, 'target');
const sha256 = value => createHash('sha256').update(value).digest('hex');
const LOCAL_PRIVATE_PREVIEW_MODE = 'loopback-local-development';

function object(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
}

export { isPublicRegistryRecord, isRealPublicBrowserBinding };

export function isLoopbackLocalProjection(env = process.env) {
  const lifecycle = env.npm_lifecycle_event || '';
  if (lifecycle === 'prebuild') return false;
  if (lifecycle === 'prebuild:local') return true;
  if (env.VIBAPP_WEB_PRIVATE_PREVIEW_MODE !== LOCAL_PRIVATE_PREVIEW_MODE) return false;
  if (env.VIBAPP_PREVIEW_LOCAL !== '1') return false;
  try {
    const origin = new URL(env.VIBAPP_PREVIEW_ORIGIN || '');
    return origin.protocol === 'http:'
      && origin.hostname === '127.0.0.1'
      && origin.port !== ''
      && origin.pathname === '/'
      && !origin.username
      && !origin.password
      && !origin.search
      && !origin.hash;
  } catch {
    return false;
  }
}

export function buildRegistryProjection(source, localDerivation = null) {
  if (!object(source) || !Array.isArray(source.records)) {
    throw new Error('Registry snapshot must contain a records array');
  }
  const records = [];
  const appIds = new Set();
  const recordIds = new Set();
  const packageDigests = new Set();
  for (const record of source.records) {
    if (isSyntheticRegistryRecord(record)) continue;
    const valid = isPublicRegistryRecord(record);
    const claimsPublicAuthority = record?.publication?.state === 'published'
      || record?.source?.visibility === 'public';
    if (!valid) {
      if (claimsPublicAuthority) {
        throw new Error('A Registry record claims public authority but fails the exact public trust policy');
      }
      continue;
    }
    if (appIds.has(record.app.id)
      || recordIds.has(record.record_id)
      || packageDigests.has(record.package.package_digest_sha256)) {
      throw new Error('The public Registry projection contains a duplicate app, record, or package digest');
    }
    appIds.add(record.app.id);
    recordIds.add(record.record_id);
    packageDigests.add(record.package.package_digest_sha256);
    records.push(record);
  }
  const recordsByApp = new Map(records.map(record => [record.app.id, record]));
  const publicBindings = [];
  const publicBindingKeys = new Set();
  for (const binding of Array.isArray(source.browser_artifact_bindings) ? source.browser_artifact_bindings : []) {
    const record = recordsByApp.get(binding?.app_id);
    if (isRealPublicBrowserBinding(record, binding)) {
      const key = `${binding.app_id}\0${binding.profile}`;
      if (publicBindingKeys.has(key)) {
        throw new Error('The public Registry projection contains a duplicate browser binding');
      }
      publicBindingKeys.add(key);
      publicBindings.push(binding);
    } else if (record && binding?.source_kind === 'verifier-promoted-candidate') {
      throw new Error('A browser binding claims verifier promotion but fails the exact public trust policy');
    }
  }
  const publicShareableAppIds = [...new Set(publicBindings.map(binding => binding.app_id))].sort();
  const localPreviewEnabled = localDerivation !== null;
  const browserPreviewRecords = localPreviewEnabled
    ? [localDerivation.previewRecord]
    : [];
  const browserArtifactBindings = localPreviewEnabled
    ? [...publicBindings, localDerivation.binding]
    : publicBindings;
  const publicAppIds = new Set(records.map(record => record.app.id));
  const publicCandidate = item => (
    typeof item?.candidate?.app_id === 'string' && publicAppIds.has(item.candidate.app_id)
  );
  const searchResponse = object(source.sample_search?.search_response) ? {
    ...source.sample_search.search_response,
    matches: Array.isArray(source.sample_search.search_response.matches)
      ? source.sample_search.search_response.matches.filter(publicCandidate)
      : [],
  } : source.sample_search?.search_response;
  const sampleSearch = object(source.sample_search) ? {
    ...source.sample_search,
    matched: Array.isArray(source.sample_search.matched)
      ? source.sample_search.matched.filter(item => publicAppIds.has(item?.record?.app?.id))
      : [],
    rejected: Array.isArray(source.sample_search.rejected)
      ? source.sample_search.rejected.filter(publicCandidate)
      : [],
    ...(searchResponse ? { search_response: searchResponse } : {}),
  } : source.sample_search;
  return {
    ...source,
    records,
    browser_artifact_bindings: browserArtifactBindings,
    browser_preview_records: browserPreviewRecords,
    ...(sampleSearch ? { sample_search: sampleSearch } : {}),
    consumer_notes: {
      ...(object(source.consumer_notes) || {}),
      browser_preview_records: localPreviewEnabled
        ? 'Private browser preview projection; loopback local development only. It has no install, publication, or public sharing authority.'
        : 'No private browser preview record is included in the production Website projection.',
      browser_derivation_state: localPreviewEnabled
        ? localDerivation.binding.attestation.verification_state
        : (publicBindings.length > 0 ? 'verified' : 'no-public-runtime-binding'),
      stage0_activation_eligible: publicBindings.some(binding => binding.attestation.stage0_activation_eligible === true),
      website_projection_mode: localPreviewEnabled ? LOCAL_PRIVATE_PREVIEW_MODE : 'production-public-only',
      website_public_record_filters: [
        '/schema_version=vibapp.registry-record.product-v0.0.1',
        '/record_id=bounded-safe-id',
        '/record_revision=positive-safe-integer',
        '/app/publisher/verification_state=verified',
        '/publication/state=published',
        '/publication/public_metadata_digest_sha256=lowercase-sha256',
        '/publication/revoked_at_utc=null',
        '/verification/status=verified',
        '/verification/revocation=not-revoked',
        '/source/github_archive/organization=vib-app',
        '/source/github_archive/source_digest_sha256=release-source-digest',
        '/source/github_archive/package_digest_sha256=release-package-digest',
        '/source/source_digest_sha256=lowercase-sha256',
        '/package/manifest_digest_sha256=lowercase-sha256',
        '/package/version=/app/version',
      ],
      website_public_shareable_app_ids: publicShareableAppIds,
      local_private_preview_enabled: localPreviewEnabled,
    },
  };
}

export async function syncProductionPublicRegistryData() {
  const registrySourceDocument = JSON.parse(await readFile(registrySource, 'utf8'));
  const productionRegistry = buildRegistryProjection(registrySourceDocument);
  // Published app bytes are retained outside the regenerated launcher tree.
  // Copy only the current registry's exact content-addressed allowlist.
  for (const binding of productionRegistry.browser_artifact_bindings || []) {
    for (const descriptor of [...binding.files, binding.attestation.artifact]) {
      const relativePath = descriptor.path.slice('/launcher/components/'.length);
      const source = join(coreRoot, 'published-components', relativePath);
      const metadata = await lstat(source);
      if (!metadata.isFile() || metadata.isSymbolicLink()) throw new Error('Published browser source is not a regular file');
      const bytes = await readFile(source);
      if (bytes.length !== descriptor.size_bytes || sha256(bytes) !== descriptor.sha256) throw new Error('Published browser source digest mismatch');
      const target = join(websiteRoot, 'public', descriptor.path.slice(1));
      await mkdir(dirname(target), { recursive: true });
      await writeFile(target, bytes);
    }
  }
  await verifyPublicProjectionArtifacts(productionRegistry, join(websiteRoot, 'public'));
  const productionRegistryBytes = Buffer.from(JSON.stringify(productionRegistry, null, 2) + '\n');
  await syncPublicRegistryDataRoot({
    registryBytes: productionRegistryBytes,
    targetRoot: stableProductionPublicDataRoot,
    productionOnly: true,
  });
  return verifyPublicRegistryDataRoot({
    targetRoot: stableProductionPublicDataRoot,
    productionOnly: true,
  });
}

export async function verifyPublicProjectionArtifacts(projection, publicRoot) {
  if (!isAbsolute(publicRoot)) throw new Error('Website public root must be absolute');
  const trustedRoot = await realpath(publicRoot);
  const recordsByApp = new Map((projection.records || []).map(record => [record.app.id, record]));
  for (const binding of projection.browser_artifact_bindings || []) {
    if (!isRealPublicBrowserBinding(recordsByApp.get(binding?.app_id), binding)) continue;
    const descriptors = [...binding.files, binding.attestation.artifact];
    const paths = new Set();
    for (const descriptor of descriptors) {
      if (paths.has(descriptor.path)) throw new Error('Public browser binding contains a duplicate artifact path');
      paths.add(descriptor.path);
      const target = join(publicRoot, descriptor.path.slice(1));
      const metadata = await lstat(target).catch(() => null);
      if (!metadata?.isFile() || metadata.isSymbolicLink()) {
        throw new Error('Public browser binding artifact is not a regular file: ' + descriptor.path);
      }
      const canonicalTarget = await realpath(target);
      if (canonicalTarget !== trustedRoot && !canonicalTarget.startsWith(trustedRoot + sep)) {
        throw new Error('Public browser binding artifact resolves outside Website public root');
      }
      const bytes = await readFile(canonicalTarget);
      if (bytes.byteLength !== descriptor.size_bytes || sha256(bytes) !== descriptor.sha256) {
        throw new Error('Public browser binding artifact bytes do not match its descriptor: ' + descriptor.path);
      }
    }
  }
}

export async function syncWebGui(env = process.env) {

const cargo = cargoPath('1.93.0', env.VIBAPP_CARGO_BIN);
if (!isAbsolute(cargo)) throw new Error('VIBAPP_CARGO_BIN must be an absolute 1.93.0 cargo path');
const version = spawnSync(cargo, ['--version'], { encoding: 'utf8', env: { ...env, RUSTUP_AUTO_INSTALL: '0' } });
if (version.status !== 0 || !version.stdout.startsWith('cargo 1.93.0 ')) {
  throw new Error('exact Cargo 1.93.0 toolchain is required');
}

const result = spawnSync(cargo, [
  'build',
  '--manifest-path', join(coreRoot, 'Cargo.toml'),
  '--target', 'wasm32-unknown-unknown',
  '--target-dir', targetRoot,
  '--release',
  '--offline',
  '--locked',
  '--jobs', '2',
], {
  cwd: coreRoot,
  env: {
    ...env,
    CARGO_INCREMENTAL: '0',
    CARGO_NET_OFFLINE: 'true',
    RUSTUP_AUTO_INSTALL: '0',
    RUSTC: join(dirname(cargo), process.platform === 'win32' ? 'rustc.exe' : 'rustc'),
  },
  encoding: 'utf8',
});
if (result.status !== 0) {
  process.stderr.write(result.stderr || result.stdout || 'WebAssembly core build failed.\n');
  process.exit(result.status || 1);
}

await rm(launcherRoot, { recursive: true, force: true });
await mkdir(launcherRoot, { recursive: true });
const guiSourceFiles = ['app.js', 'styles.css', 'favicon.svg'];
for (const name of guiSourceFiles) {
  await copyFile(join(desktopUi, name), join(launcherRoot, name));
}
const cacheDigest = createHash('sha256');
cacheDigest.update(await readFile(join(desktopUi, 'app.js')));
cacheDigest.update(await readFile(join(coreRoot, 'web-bridge.js')));
const cacheVersion = cacheDigest.digest('hex').slice(0, 16);
const bridgeSpecifier = `web-bridge.js?v=${cacheVersion}`;
const sourceHtml = await readFile(join(desktopUi, 'index.html'), 'utf8');
const webHtml = sourceHtml
  .replace('<span data-locale-key="nav_apps">Library</span>', '<span data-locale-key="nav_apps">Store</span>')
  .replace('<small data-locale-key="app_mode_label">LOCAL</small>', '<small data-locale-key="app_mode_label">WEB · WASM</small>')
  .replace('<script src="app.js" defer></script>', `<script type="module" src="${bridgeSpecifier}"></script>\n    <script src="app.js?v=${cacheVersion}" defer></script>`);
if (webHtml === sourceHtml || !webHtml.includes(bridgeSpecifier)) {
  throw new Error('Desktop index no longer matches the bounded Web adapter transform');
}
await writeFile(join(launcherRoot, 'index.html'), webHtml, 'utf8');
await copyFile(join(coreRoot, 'web-bridge.js'), join(launcherRoot, 'web-bridge.js'));
await copyFile(join(coreRoot, 'runtime-cache-worker.js'), join(launcherRoot, 'runtime-cache-worker.js'));
await copyFile(join(coreRoot, 'app-runtime-worker.js'), join(launcherRoot, 'app-runtime-worker.js'));
await copyFile(join(coreRoot, 'foreground-session.mjs'), join(launcherRoot, 'foreground-session.mjs'));
await copyFile(join(coreRoot, 'runtime-integrity.mjs'), join(launcherRoot, 'runtime-integrity.mjs'));
await copyFile(join(coreRoot, 'invalid-bootstrap-audit.mjs'), join(launcherRoot, 'invalid-bootstrap-audit.mjs'));
await copyFile(
  join(targetRoot, 'wasm32-unknown-unknown', 'release', 'vibapp_web_client_core.wasm'),
  join(launcherRoot, 'vibapp_web_client_core.wasm'),
);

await syncProductionPublicRegistryData();

const registrySourceDocument = JSON.parse(await readFile(registrySource, 'utf8'));
const localDerivation = isLoopbackLocalProjection(env) ? await deriveBrowserComponent(launcherRoot) : null;
const registry = buildRegistryProjection(registrySourceDocument, localDerivation);
await verifyPublicProjectionArtifacts(registry, join(websiteRoot, 'public'));
const registryBytes = Buffer.from(JSON.stringify(registry, null, 2) + '\n');
// Replace the Website snapshot and its locator index as one validated generation.
// Local preview mode may add a private browser record here, while the separate
// stable production root above always remains production-public-only.
await syncPublicRegistryDataRoot({
  registryBytes,
  targetRoot: join(websiteRoot, 'public', 'data'),
});

const guiSyncManifest = {
  schema_version: 'vibapp.web-gui-sync.experimental-v1',
  source: 'artifacts/desktop/ui',
  adapter: {
    index_only: true,
    mode_label: 'WEB · WASM',
    cache_version: cacheVersion,
    bridge: bridgeSpecifier,
  },
  files: {},
};
for (const name of guiSourceFiles) {
  const [sourceBytes, generatedBytes] = await Promise.all([
    readFile(join(desktopUi, name)),
    readFile(join(launcherRoot, name)),
  ]);
  if (!sourceBytes.equals(generatedBytes)) throw new Error(name + ' drifted from Desktop GUI source');
  guiSyncManifest.files[name] = {
    source_sha256: sha256(sourceBytes),
    generated_sha256: sha256(generatedBytes),
    exact_match: true,
  };
}
guiSyncManifest.files['index.html'] = {
  source_sha256: sha256(Buffer.from(sourceHtml)),
  generated_sha256: sha256(Buffer.from(webHtml)),
  exact_match: false,
  bounded_adapter_transform: true,
};
await writeFile(join(launcherRoot, 'gui-sync-manifest.json'), JSON.stringify(guiSyncManifest, null, 2) + '\n', 'utf8');

// RoomHash runs in the trusted Website parent origin, never in the isolated
// launcher/guest preview origin. The dedicated synchronizer verifies the
// vendored bundle's pinned provenance before placing it outside `launcher`.
const roomHashSync = spawnSync(process.execPath, [join(coreRoot, 'sync-roomhash-browser.mjs')], {
  cwd: coreRoot,
  env: { ...env },
  encoding: 'utf8',
});
if (roomHashSync.status !== 0) {
  process.stderr.write(roomHashSync.stderr || roomHashSync.stdout || 'RoomHash browser adapter sync failed.\n');
  process.exit(roomHashSync.status || 1);
}

process.stdout.write(launcherRoot + '\n');
}

const directInvocation = process.argv[1]
  && import.meta.url === pathToFileURL(resolve(process.argv[1])).href;
if (directInvocation) {
  const arguments_ = process.argv.slice(2);
  if (arguments_.length === 0) {
    await syncWebGui();
  } else if (arguments_.length === 1 && arguments_[0] === '--production-public-registry-only') {
    const receipt = await syncProductionPublicRegistryData();
    process.stdout.write(JSON.stringify(receipt) + '\n');
  } else {
    throw new Error('usage: sync-web-gui.mjs [--production-public-registry-only]');
  }
}
