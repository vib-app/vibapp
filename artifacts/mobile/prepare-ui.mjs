import { createHash } from 'node:crypto';
import { cp, lstat, mkdir, mkdtemp, readFile, readdir, rename, writeFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { verifyPublicRegistryDataRoot } from '../web-client-core/sync-public-package-locators.mjs';

const root = dirname(fileURLToPath(import.meta.url));
const publicRoot = resolve(root, '../product-platform/website/public');
await verifyPublicRegistryDataRoot({ targetRoot: join(publicRoot, 'data'), productionOnly: true });
await mkdir(join(root, 'output'), { recursive: true });
const output = await mkdtemp(join(root, 'output', 'ui-staging-'));
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');

// The same synchronizer used by the website owns the generated bridge and
// integrity-bound public bindings. Never fabricate a mobile-only app listing.
for (const name of ['app.js', 'styles.css', 'favicon.svg']) {
  const source = await readFile(resolve(root, '../desktop/ui', name));
  const projected = await readFile(join(publicRoot, 'launcher', name));
  if (sha256(source) !== sha256(projected)) throw new Error(`Shared GUI projection is stale: ${name}. Run sync-web-gui.mjs first.`);
}
await mkdir(join(output, 'launcher'), { recursive: true });
for (const name of ['app.js', 'styles.css', 'favicon.svg', 'index.html', 'gui-sync-manifest.json',
  'web-bridge.js', 'invalid-bootstrap-audit.mjs', 'runtime-integrity.mjs',
  'foreground-session.mjs', 'app-runtime-worker.js', 'vibapp_web_client_core.wasm']) {
  await cp(join(publicRoot, 'launcher', name), join(output, 'launcher', name));
}
await cp(join(publicRoot, 'data'), join(output, 'data'), { recursive: true });
const snapshot = JSON.parse(await readFile(join(output, 'data', 'registry.snapshot.json'), 'utf8'));
const copied = new Set();
let runtimeBytes = 0;
for (const binding of snapshot.browser_artifact_bindings) {
  for (const descriptor of [...binding.files, binding.attestation.artifact]) {
    if (copied.has(descriptor.path)) continue;
    const path = join(publicRoot, descriptor.path.slice(1));
    const metadata = await lstat(path);
    if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.size !== descriptor.size_bytes) throw new Error('Mobile runtime artifact is not a bounded regular file');
    const bytes = await readFile(path);
    if (sha256(bytes) !== descriptor.sha256) throw new Error('Mobile runtime artifact digest mismatch');
    runtimeBytes += bytes.length;
    if (runtimeBytes > 64 * 1024 * 1024) throw new Error('Mobile bundled runtimes exceed 64 MiB');
    const destination = join(output, descriptor.path.slice(1));
    await mkdir(dirname(destination), { recursive: true });
    await writeFile(destination, bytes);
    copied.add(descriptor.path);
  }
}
for (const name of await readdir(join(root, 'host'))) await cp(join(root, 'host', name), join(output, name));
const indexPath = join(output, 'launcher/index.html');
let index = await readFile(indexPath, 'utf8');
index = index.replace('</head>', '<link rel="stylesheet" href="/mobile-launcher.css"></head>');
await writeFile(indexPath, index);
await writeFile(join(output, 'mobile-capabilities.json'), JSON.stringify({
  schema_version: 'vibapp.mobile-capabilities.v1',
  platform: 'android', execution_profile: 'web-runtime', background: 'foreground-only',
  shared_gui_sha256: sha256(await readFile(join(output, 'launcher/app.js'))),
  runtime: 'verified-component-derivation-in-dedicated-worker',
  local_codeagent: false, desktop_service_runtime: false, remote_service_connected: false,
  package_delivery: 'bundled-verified-public-registry',
}, null, 2) + '\n');
const destination = join(root, 'dist');
const previous = await lstat(destination).catch(error => { if (error.code === 'ENOENT') return null; throw error; });
if (previous) {
  if (!previous.isDirectory() || previous.isSymbolicLink()) throw new Error('Mobile dist must be an ordinary generated directory');
  await rename(destination, join(root, 'output', `previous-dist-${Date.now()}`));
}
await rename(output, destination);
console.log(`Prepared shared mobile assets: ${destination}; ${snapshot.browser_artifact_bindings.length} verified runtime bindings`);
