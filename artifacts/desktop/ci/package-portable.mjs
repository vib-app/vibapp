// Trusted client packaging only; never processes generated app source.
import { cpSync, mkdirSync, readdirSync, readFileSync, writeFileSync, statSync, lstatSync } from 'node:fs';
import { join, dirname, resolve } from 'node:path';
import { createHash } from 'node:crypto';
import { execFileSync, spawn } from 'node:child_process';
import { computeDesktopBuildInputs } from '../scripts/desktop-build-inputs.mjs';
import { packageWindowsInstaller } from './windows-installer.mjs';

const sha = path => createHash('sha256').update(readFileSync(path)).digest('hex');
const run = (file, args, options = {}) => execFileSync(file, args, { stdio: 'inherit', timeout: 120000, ...options });
export async function packagePortableClient({ root, platformKey, python, node, roomhash, inspector, desktopTarget, pins, sourceCommit }) {
  const windows = platformKey === 'win32-x64';
  if (!windows && platformKey !== 'linux-x64') throw Error('Unsupported portable target');
  const exe = windows ? '.exe' : '';
  const packageRoot = resolve(root, 'generated/client-staging', windows ? 'VibApp' : 'vibapp');
  mkdirSync(packageRoot, { recursive: true });
  const resources = join(packageRoot, 'resources');
  const bin = windows ? packageRoot : join(packageRoot, 'bin');
  const copy = (source, target) => {
    mkdirSync(dirname(target), { recursive: true });
    cpSync(source, target, { recursive: true, verbatimSymlinks: true, filter: path => !/(?:^|[/\\])(?:__pycache__|\.git)(?:[/\\]|$)|\.py[co]$/.test(path) });
  };
  const receipt = await computeDesktopBuildInputs(join(root, 'artifacts/desktop'));
  for (const name of ['vibapp-launcher', 'vibapp-runtime']) {
    const source = join(desktopTarget, 'release', name + exe);
    if (!readFileSync(source).includes(Buffer.from('VIBAPP_DESKTOP_BUILD_INPUTS_SHA256=' + receipt.sha256))) throw Error('Binary/input receipt mismatch: ' + name);
    copy(source, join(bin, name + exe));
  }
  // Only source code and explicitly named public fixtures are shipped; no env,
  // local settings, credentials, test output, private apps or development data.
  const modules = ['app-builder', 'cloud-agent', 'codeagent-adapter', 'codeagent-launcher', 'registry',
    'registry-store', 'orchestrator', 'need-analyzer', 'source-archive'];
  for (const module of modules) {
    const directory = join(root, 'artifacts', module);
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      if (entry.isFile() && entry.name.endsWith('.py')) copy(join(directory, entry.name), join(resources, module, entry.name));
    }
  }
  for (const path of ['app-builder/schemas', 'cloud-agent/schemas', 'cloud-agent/starter',
    'cloud-agent/skills', 'cloud-agent/fixtures/dry-run', 'codeagent-launcher/docker',
    'registry/fixtures/catalog.json', 'product-config/llm.env.example', 'runtime-daemon/vibapp_daemon']) {
    copy(join(root, 'artifacts', path), join(resources, path));
  }
  copy(join(root, 'wit'), join(packageRoot, 'wit'));
  copy(join(root, 'schemas'), join(packageRoot, 'schemas'));
  copy(inspector, join(packageRoot, 'libexec', 'vibapp-service-runtime' + exe));
  copy(inspector, join(resources, 'runtime-daemon/service-runtime', 'vibapp-service-runtime' + exe));
  copy(join(root, 'artifacts/product-platform/registry/generated/production-public-data'), join(resources, 'public-registry/data'));
  copy(join(root, 'artifacts/roomhash-transport/roomhash-host.mjs'), join(resources, 'roomhash/roomhash-host.mjs'));
  copy(join(root, 'artifacts/roomhash-collaboration/src'), join(resources, 'roomhash/collaboration'));
  copy(node, join(resources, 'roomhash/node' + exe));
  for (const name of ['package.json', 'package-lock.json', 'src', 'node_modules']) copy(join(roomhash, name), join(resources, 'roomhash/current/headless', name));
  copy(dirname(windows ? python : dirname(python)), join(resources, 'python'));
  const packagedPython = join(resources, windows ? 'python/python.exe' : 'python/bin/python3.13');
  const launcher = join(bin, 'vibapp-launcher' + exe);
  const runtime = join(bin, 'vibapp-runtime' + exe);
  run(packagedPython, ['-I', '-B', '-c', 'import tomllib,ssl,sqlite3,ctypes,multiprocessing;import sys;sys.path.insert(0,sys.argv[1]);import vibapp_daemon.core,vibapp_daemon.cli;sys.path.insert(0,sys.argv[2]);from descriptor_reconciliation import descriptor_inspector_preflight;descriptor_inspector_preflight();print("Packaged Python and verifier ready")', join(resources, 'runtime-daemon'), join(resources, 'app-builder')]);
  run(launcher, ['--help'], { timeout: 20000 });
  const component = join(root, 'artifacts/desktop/runtime-apps/hello/component.wasm');
  const result = JSON.parse(execFileSync(runtime, ['--component', component, '--expected-sha256', sha(component)], { encoding: 'utf8', timeout: 20000, maxBuffer: 524288 }));
  if (result.component_sha256 !== sha(component)) throw Error('Packaged Rust app runtime smoke failed');
  run(process.execPath, [join(root, 'artifacts/desktop/scripts/tests/smoke_packaged_roomhash.mjs'), packageRoot], { timeout: 90000 });
  if (!windows) {
    // Exercise a real WebKit/GTK window in the runner's virtual display.
    const child = spawn('xvfb-run', ['-a', launcher], { stdio: ['ignore', 'ignore', 'pipe'], env: { ...process.env, WEBKIT_DISABLE_DMABUF_RENDERER: '1' }, detached: true });
    let errors = '';
    child.stderr.on('data', bytes => { errors = (errors + bytes).slice(-8192); });
    await new Promise((ok, fail) => {
      const timer = setTimeout(ok, 6000);
      child.once('error', error => { clearTimeout(timer); fail(error); });
      child.once('exit', code => { clearTimeout(timer); fail(Error('Launcher exited before GUI smoke: ' + code + ' ' + errors)); });
    }).finally(() => { try { process.kill(-child.pid, 'SIGTERM'); } catch {} });
  }
  mkdirSync(join(resources, 'build-provenance'), { recursive: true });
  writeFileSync(join(resources, 'build-provenance/desktop-inputs.json'), JSON.stringify(receipt) + '\n');
  const output = join(root, 'generated/client-release');
  mkdirSync(output, { recursive: true });
  let artifactName;
  let installer = null;
  if (windows) {
    artifactName = 'VibApp-Windows-x64-Setup.exe';
    installer = packageWindowsInstaller({ root, packageRoot, output: join(output, artifactName) });
  } else {
    artifactName = 'VibApp-Linux-x64.deb';
    const debRoot = join(root, 'generated/client-deb');
    copy(packageRoot, join(debRoot, 'opt/vibapp'));
    const write = (path, text, mode = 0o644) => { mkdirSync(dirname(path), { recursive: true }); writeFileSync(path, text, { mode }); };
    write(join(debRoot, 'DEBIAN/control'), 'Package: vibapp\nVersion: 0.1.0-preview.2\nSection: utils\nPriority: optional\nArchitecture: amd64\nMaintainer: VibApp <support@vibapp.ai>\nDepends: libwebkit2gtk-4.1-0, libgtk-3-0, libssl3, ca-certificates\nDescription: VibApp app store and Rust component launcher (preview)\n');
    write(join(debRoot, 'usr/share/applications/vibapp.desktop'), '[Desktop Entry]\nType=Application\nName=VibApp\nComment=Find it, Vibe it\nExec=/opt/vibapp/bin/vibapp-launcher %u\nIcon=vibapp\nTerminal=false\nCategories=Utility;Development;\nMimeType=x-scheme-handler/vibapp;\n');
    copy(join(root, 'artifacts/desktop/src-tauri/icons/icon.png'), join(debRoot, 'usr/share/icons/hicolor/512x512/apps/vibapp.png'));
    run('dpkg-deb', ['--root-owner-group', '--build', debRoot, join(output, artifactName)]);
    run('dpkg-deb', ['--info', join(output, artifactName)]);
  }
  const artifact = { name: artifactName, sha256: sha(join(output, artifactName)), size: statSync(join(output, artifactName)).size };
  const manifestName = 'release-manifest-' + platformKey + '.json';
  const manifestPath = join(output, manifestName);
  writeFileSync(manifestPath, JSON.stringify({ schema_version: 'vibapp.client-release.v1', source_commit: sourceCommit, platform: windows ? 'Windows-x64' : 'Linux-x64', signing: 'unsigned-preview', dependencies: pins, installer, inspector_sha256: sha(inspector), desktop_inputs: receipt, checks: ['packaged-python-imports', 'descriptor-pin', 'launcher-help', 'rust-component-render', 'roomhash-start-stop', ...(!windows ? ['gtk-webkit-window-start'] : [])], artifact }, null, 2) + '\n');
  writeFileSync(join(output, 'SHA256SUMS-' + platformKey), artifact.sha256 + '  ' + artifactName + '\n' + sha(manifestPath) + '  ' + manifestName + '\n');
}
