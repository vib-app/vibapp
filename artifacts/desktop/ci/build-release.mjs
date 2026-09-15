import { createHash } from 'node:crypto';
import { execFileSync, spawnSync } from 'node:child_process';
import { mkdirSync, readFileSync, writeFileSync, statSync, symlinkSync, cpSync } from 'node:fs';
import { resolve, join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { stampInspectorPin, releaseTag } from './release-policy.mjs';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');
process.chdir(root);
if (process.platform !== 'darwin' || process.arch !== 'arm64') throw new Error('Only macOS arm64 is release-qualified');
if (process.env.GITHUB_ACTIONS !== 'true') throw new Error('Run in a disposable GitHub Actions checkout, not the working tree');
if (process.env.RELEASE_TAG) releaseTag(process.env.RELEASE_TAG);
const pins = JSON.parse(readFileSync('artifacts/desktop/ci/release-dependencies.json'));
const sha = file => createHash('sha256').update(readFileSync(file)).digest('hex');
const run = (file, args, options = {}) => execFileSync(file, args, { cwd: root, stdio: 'inherit', timeout: 1800000, ...options });
const deps = resolve('generated/release-deps');
mkdirSync(deps, { recursive: true });
for (const [name, pin] of Object.entries({ python: pins.python, node: pins.node })) {
  const archive = join(deps, `${name}.tar.gz`);
  run('/usr/bin/curl', ['--fail', '--location', '--retry', '2', '--max-time', '180', '--max-filesize', '104857600', '--output', archive, pin.url]);
  if (sha(archive) !== pin.sha256) throw new Error(`${name} archive checksum mismatch`);
  const target = join(deps, name);
  mkdirSync(target);
  run('/usr/bin/tar', ['-xzf', archive, '-C', target, '--strip-components=1']);
}
const python = join(deps, 'python/bin/python3.13');
const node = join(deps, 'node/bin/node');
const roomhash = join(deps, 'RoomHash/headless');
const roomhashCommit = execFileSync('git', ['-C', roomhash, 'rev-parse', 'HEAD'], { encoding: 'utf8' }).trim();
if (roomhashCommit !== pins.roomhash_commit) throw new Error('RoomHash source revision mismatch');
run('npm', ['ci', '--omit=dev', '--no-audit', '--no-fund'], { cwd: roomhash });
mkdirSync('artifacts/product-platform/website/public', { recursive: true });
run(node, ['artifacts/web-client-core/sync-web-gui.mjs', '--production-public-registry-only']);

// This is the trusted product host build, NOT the untrusted generated-app builder.
// Fetch with locked checksums first, then compile offline without release credentials.
run('rustup', ['toolchain', 'install', pins.rust, '--profile', 'minimal']);
const rustBin = execFileSync('rustup', ['which', '--toolchain', pins.rust, 'rustc'], { encoding: 'utf8' }).trim();
const rustDir = dirname(rustBin);
const cargo = join(rustDir, 'cargo');
const baseEnv = { ...process.env, RUSTC: rustBin, RUSTDOC: join(rustDir, 'rustdoc'), CARGO_BUILD_JOBS: '2', RUSTUP_AUTO_INSTALL: '0' };
delete baseEnv.GH_TOKEN;
delete baseEnv.GITHUB_TOKEN;
const serviceManifest = 'artifacts/runtime-daemon/service-runtime/Cargo.toml';
const desktopManifest = 'artifacts/desktop/src-tauri/Cargo.toml';
for (const manifest of [serviceManifest, desktopManifest]) {
  run(cargo, ['fetch', '--locked', '--manifest-path', manifest], { env: { ...baseEnv, CARGO_NET_OFFLINE: 'false' } });
}
const serviceTarget = resolve('artifacts/runtime-daemon/target-service-1_98');
run(cargo, ['build', '--locked', '--offline', '--release', '--manifest-path', serviceManifest], { env: { ...baseEnv, CARGO_TARGET_DIR: serviceTarget } });
const inspector = join(serviceTarget, 'release/vibapp-service-runtime');
run('/usr/bin/codesign', ['--force', '--sign', '-', '--timestamp=none', '--options', 'runtime', '--entitlements', 'artifacts/desktop/packaging/macos/Runtime.entitlements.plist', inspector]);
if (statSync(inspector).size > 16 * 1024 * 1024) throw new Error('Inspector exceeds verifier executable budget');
const component = resolve('artifacts/desktop/runtime-apps/hello/component.wasm');
const inspectionArgs = ['--inspect-descriptor', '--component', component, '--expected-sha256', sha(component), '--world', 'ui-only-reference'];
const inspection = JSON.parse(execFileSync(inspector, inspectionArgs, { encoding: 'utf8', timeout: 20000, maxBuffer: 262144 }));
if (inspection.component_sha256 !== sha(component) || inspection.descriptor?.id !== 'ai.vibapp.hello' || inspection.isolation?.ambient_wasi_linked !== false) throw new Error('Real inspector smoke failed');
const wrongDigest = [...inspectionArgs];
wrongDigest[4] = '0'.repeat(64);
const rejected = spawnSync(inspector, wrongDigest, { encoding: 'utf8', timeout: 20000, maxBuffer: 262144 });
if (rejected.status !== 1 || !rejected.stderr.includes('INSPECT_REJECTED')) throw new Error('Inspector did not reject wrong artifact digest');

// Bind the verifier to THIS trusted CI build's signed executable, before the
// launcher computes its source-input receipt. No runtime override or TOFU.
const reconciliation = 'artifacts/app-builder/descriptor_reconciliation.py';
const original = readFileSync(reconciliation, 'utf8');
writeFileSync(reconciliation, stampInspectorPin(original, sha(inspector)));
run(python, ['-I', '-B', '-c', 'import sys;sys.path.insert(0,sys.argv[1]);from descriptor_reconciliation import descriptor_inspector_preflight;descriptor_inspector_preflight()', resolve('artifacts/app-builder')]);

const desktopTarget = resolve('artifacts/desktop/target');
const buildEnv = { ...baseEnv, CARGO_TARGET_DIR: desktopTarget };
run(cargo, ['test', '--locked', '--offline', '--release', '--manifest-path', desktopManifest, '--bin', 'vibapp-launcher', 'native_platform::tests'], { env: buildEnv });
run(cargo, ['build', '--locked', '--offline', '--release', '--manifest-path', desktopManifest, '--bin', 'vibapp-launcher', '--bin', 'vibapp-runtime'], { env: buildEnv });
run('/bin/sh', ['artifacts/desktop/scripts/package-macos.sh', 'release'], { env: {
  ...buildEnv, VIBAPP_SERVICE_RUNTIME_BIN: inspector, VIBAPP_ROOMHASH_ROOT: dirname(roomhash),
  VIBAPP_ROOMHASH_NODE_BIN: node, VIBAPP_PYTHON_BIN: python, VIBAPP_BUNDLED_PYTHON_ROOT: join(deps, 'python'),
} });
const bundle = resolve('artifacts/desktop/dist/VibApp.app');
run(join(bundle, 'Contents/MacOS/vibapp-launcher'), ['--help'], { env: { PATH: '/usr/bin:/bin' }, timeout: 20000 });
run(node, ['artifacts/desktop/scripts/tests/smoke_packaged_roomhash.mjs', bundle], { timeout: 90000 });
run(join(bundle, 'Contents/Resources/python/bin/python3.13'), ['-I', '-B', '-c',
  'import sys;sys.path.insert(0,sys.argv[1]);import vibapp_daemon;print("Packaged daemon imports passed")', join(bundle, 'Contents/Resources/runtime-daemon')]);

const output = resolve('generated/client-release');
mkdirSync(output, { recursive: true });
const diskRoot = resolve('generated/client-dmg');
mkdirSync(diskRoot);
cpSync(bundle, join(diskRoot, 'VibApp.app'), { recursive: true, verbatimSymlinks: true });
symlinkSync('/Applications', join(diskRoot, 'Applications'));
const dmg = join(output, 'VibApp-macOS-Apple-Silicon.dmg');
run('/usr/bin/hdiutil', ['create', '-volname', 'VibApp', '-srcfolder', diskRoot, '-ov', '-format', 'UDZO', dmg], { timeout: 180000 });
run('/usr/bin/hdiutil', ['verify', dmg], { timeout: 60000 });
const receipt = {
  schema_version: 'vibapp.client-release.v1', source_commit: process.env.GITHUB_SHA,
  platform: 'macOS-arm64', signing: 'ad-hoc-not-notarized', dependencies: pins,
  inspector_sha256: sha(inspector), desktop_inputs: JSON.parse(readFileSync(join(bundle, 'Contents/Resources/build-provenance/desktop-inputs.json'))),
  artifact: { name: 'VibApp-macOS-Apple-Silicon.dmg', sha256: sha(dmg), size: statSync(dmg).size },
};
const receiptPath = join(output, 'release-manifest.json');
writeFileSync(receiptPath, JSON.stringify(receipt, null, 2) + '\n');
writeFileSync(join(output, 'SHA256SUMS'), `${sha(dmg)}  VibApp-macOS-Apple-Silicon.dmg\n${sha(receiptPath)}  release-manifest.json\n`);
