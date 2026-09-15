import { spawnSync } from 'node:child_process';
import { readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { cargoPath } from './toolchains.mjs';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const flags = new Set(process.argv.slice(2));
if ([...flags].some(flag => !['--rust', '--sync-web'].includes(flag))) throw new Error('Usage: node scripts/check.mjs [--rust] [--sync-web]');
const env = { ...process.env, PYTHONDONTWRITEBYTECODE: '1', RUSTUP_AUTO_INSTALL: '0', CARGO_BUILD_JOBS: '2' };
const python = env.VIBAPP_PYTHON_BIN || 'python3';
let passed = 0;
function run(label, command, args, extraEnv = {}) {
  process.stdout.write(`Checking ${label}\n`);
  const result = spawnSync(command, args, { cwd: root, env: { ...env, ...extraEnv }, stdio: 'inherit', timeout: 300_000 });
  if (result.error || result.status !== 0) throw new Error(`${label} failed: ${result.error?.message || result.signal || result.status}`);
  passed++;
}
run('Python >= 3.11', python, ['-c', 'import sys; assert sys.version_info >= (3, 11), "Python >= 3.11 is required"']);
if (flags.has('--sync-web')) run('shared GUI local-preview generation', process.execPath, ['artifacts/web-client-core/sync-web-gui.mjs'], {
  VIBAPP_WEB_PRIVATE_PREVIEW_MODE: 'loopback-local-development', VIBAPP_PREVIEW_LOCAL: '1',
  VIBAPP_PREVIEW_ORIGIN: 'http://127.0.0.1:4174',
});
// Sequential on purpose: provider tests exercise the shared host-user budget.
for (const suite of ['cloud-agent/tests', 'codeagent-adapter/tests', 'codeagent-launcher/tests', 'orchestrator/tests', 'app-builder/tests', 'registry/tests', 'registry-store/tests', 'source-archive/tests', 'product-integration']) {
  run(suite, python, ['-B', '-m', 'unittest', 'discover', '-s', `artifacts/${suite}`, '-p', 'test_*.py', '-q']);
}
run('desktop package runtime contract', python, ['-B', '-m', 'unittest', 'discover', '-s',
  'artifacts/desktop/scripts/tests', '-p', 'test_package_macos_runtime_contract.py', '-q']);
for (const suite of ['desktop/ui/tests', 'roomhash-collaboration/test', 'roomhash-transport/test', 'web-product-backend/tests']) {
  const files = readdirSync(join(root, 'artifacts', suite)).filter(name => name.endsWith('.test.mjs')).sort();
  run(suite, process.execPath, ['--test', '--test-reporter=dot', ...files.map(name => `artifacts/${suite}/${name}`)]);
}
run('browser foreground sessions', process.execPath, ['--test', '--test-reporter=dot', 'artifacts/web-client-core/tests/foreground-session.test.mjs']);
run('public Registry projection', process.execPath, ['--test', '--test-reporter=dot', 'artifacts/web-client-core/tests/public-package-locator-sync.test.mjs']);
if (flags.has('--sync-web')) run('web isolation and Component contract', process.execPath, ['--test', '--test-reporter=dot',
  'artifacts/web-preview-host/test/two-origin.test.mjs', 'artifacts/web-client-core/tests/contract.test.mjs']);
if (flags.has('--rust')) {
  const cargo = cargoPath('1.98.0', env.VIBAPP_DESKTOP_CARGO_BIN);
  run('service runtime regression', cargo, ['test', '--manifest-path', 'artifacts/runtime-daemon/service-runtime/Cargo.toml',
    '--target-dir', 'artifacts/runtime-daemon/target-service-1_98', '--offline', '--locked', '-j', '2',
    '--', '--test-threads=1'], { RUSTC: join(dirname(cargo), process.platform === 'win32' ? 'rustc.exe' : 'rustc') });
  run('desktop runtime regression', cargo, ['test', '--manifest-path', 'artifacts/desktop/src-tauri/Cargo.toml',
    '--target-dir', 'artifacts/desktop/target', '--offline', '--locked', '-j', '2', '--bin', 'vibapp-launcher',
    '--', '--test-threads=1'], { RUSTC: join(dirname(cargo), process.platform === 'win32' ? 'rustc.exe' : 'rustc') });
}
process.stdout.write(`PASS: ${passed} checks. This is regression evidence, not live-provider or application acceptance.\n`);
