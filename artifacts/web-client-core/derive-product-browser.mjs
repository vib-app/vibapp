// Product-only deterministic derivation. It does not promote or publish output.
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { readFile, readdir, mkdir, realpath, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { dirname, join, isAbsolute, resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { canonicalJson } from './runtime-integrity.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const VERSION = '1.15.4';
export const POLICY = 'vibapp.product-browser.stateless-v1';
const hash = value => createHash('sha256').update(value).digest('hex');
const jsonBytes = value => Buffer.from(canonicalJson(value) + '\n');
function requireValue(value, reason) { if (!value) throw new Error('browser-derive:' + reason); }
function run(command, args) {
  const result = spawnSync(command, args, { encoding: 'utf8', timeout: 120000, maxBuffer: 2 * 1024 * 1024,
    env: { PATH: process.env.PATH, npm_config_offline: 'true', NODE_OPTIONS: '--max-old-space-size=512' } });
  requireValue(result.status === 0, 'tool-failed:' + String(result.stderr).slice(0, 500));
  return result.stdout.trim();
}
async function jcoTool() {
  const executable = await realpath(process.env.VIBAPP_JCO_BIN || run('npm', ['exec', '--offline', '--yes=false',
    '--package=@bytecodealliance/jco@' + VERSION, '--', 'which', 'jco']));
  requireValue(isAbsolute(executable) && run(executable, ['--version']) === VERSION, 'jco-version');
  const packageBytes = await readFile(join(dirname(dirname(executable)), 'package.json'));
  const pkg = JSON.parse(packageBytes);
  requireValue(pkg.name === '@bytecodealliance/jco' && pkg.version === VERSION, 'jco-identity');
  return { executable, package: pkg.name, version: VERSION, entry_sha256: hash(await readFile(executable)),
    package_json_sha256: hash(packageBytes), resolution: 'npm-cache-offline' };
}
export async function deriveProductBrowser(componentPath, outputRoot) {
  requireValue(isAbsolute(componentPath) && isAbsolute(outputRoot), 'absolute-paths-required');
  const canonical = await readFile(componentPath);
  requireValue(canonical.length > 8 && canonical.length <= 16 * 1024 * 1024
    && canonical.subarray(0, 8).equals(Buffer.from([0, 97, 115, 109, 13, 0, 1, 0])), 'canonical-component');
  const tool = await jcoTool();
  const scratch = await mkdtemp(join(tmpdir(), 'vibapp-browser-derive-'));
  let generated, modules;
  try {
    run(tool.executable, ['transpile', componentPath, '-o', scratch, '--name', 'app', '--no-typescript', '--no-wasi-shim', '--instantiation', 'sync', '-q']);
    const names = (await readdir(scratch)).sort();
    requireValue(names.includes('app.js') && names.length <= 16 && names.every(name => /^app(?:\.core\d*\.wasm|\.js)$/.test(name)), 'output-inventory');
    generated = await readFile(join(scratch, 'app.js'), 'utf8');
    modules = {};
    for (const name of names.filter(name => name.endsWith('.wasm'))) {
      const bytes = await readFile(join(scratch, name));
      requireValue(bytes.length <= 1024 * 1024, 'core-size');
      modules[name] = bytes.toString('base64');
    }
  } finally { await rm(scratch, { recursive: true, force: true }); }
  const imports = [...new Set([...generated.matchAll(/imports\['([^']+)'\]/g)].map(match => match[1]))].sort();
  const allowed = ['clock', 'host-info', 'kv', 'log', 'settings'].map(name => 'vibapp:experimental-v0/' + name).sort();
  requireValue(canonicalJson(imports) === canonicalJson(allowed), 'imports-not-ui-world');
  requireValue(!/\b(?:fetch|importScripts)\s*\(|\bimport\s*\(/.test(generated) && !generated.includes('data:') && !generated.includes('blob:'), 'ambient-loader');
  const adapter = await readFile(join(here, 'product-host-adapter.mjs'));
  const adapterName = 'host-adapter-' + hash(adapter) + '.mjs';
  // Compile modules once, instantiate fresh linear memories for EACH guest call.
  // Guest RAM is ephemeral per ABI; old bump allocators cannot leak across ticks.
  const wrapper = `\nimport * as host from './${adapterName}';
const coreBytes = ${JSON.stringify(modules)};
const coreModules = new Map(Object.entries(coreBytes).map(([name, value]) => [name,
  new WebAssembly.Module(Uint8Array.from(atob(value), char => char.charCodeAt(0))) ]));
const hostImports = Object.fromEntries(${JSON.stringify(allowed)}.map(name => [name, host]));
function call(name, args) {
  const instance = instantiate(name => { if (!coreModules.has(name)) throw new Error('unknown-core-module'); return coreModules.get(name); }, hostImports);
  if (!instance.guest || typeof instance.guest[name] !== 'function') throw new Error('missing-guest-export');
  return instance.guest[name](...args);
}
export const guest = Object.freeze(Object.fromEntries(['describe','getSettingsSchema','validateSettings','handleEvent','health','migrate'].map(name => [name, (...args) => call(name, args)])));
`;
  const entry = Buffer.from(generated + wrapper);
  requireValue(entry.length <= 4 * 1024 * 1024, 'entry-size');
  const entryName = 'app-' + hash(entry) + '.jco.mjs';
  const descriptor = (name, bytes) => ({ path: 'web-runtime/' + name, media_type: 'text/javascript', sha256: hash(bytes), size_bytes: bytes.length });
  const files = [descriptor(entryName, entry), descriptor(adapterName, adapter)].sort((a, b) => a.path.localeCompare(b.path));
  const attestation = { schema_version: 'vibapp.product-browser-derivation.v1', policy: POLICY,
    profile: 'web-runtime', canonical_component_sha256: hash(canonical), canonical_component_size_bytes: canonical.length,
    tool: Object.fromEntries(Object.entries(tool).filter(([name]) => name !== 'executable')),
    imports: allowed, files, guest_memory: 'fresh-instance-per-call', persistence: 'none', background: 'foreground-only' };
  const attestationBytes = jsonBytes(attestation);
  const attestationName = 'derivation-' + hash(attestationBytes) + '.json';
  await mkdir(join(outputRoot, 'web-runtime'), { recursive: true });
  await writeFile(join(outputRoot, 'web-runtime', entryName), entry, { flag: 'wx' });
  await writeFile(join(outputRoot, 'web-runtime', adapterName), adapter, { flag: 'wx' });
  await writeFile(join(outputRoot, 'web-runtime', attestationName), attestationBytes, { flag: 'wx' });
  return { profile: 'web-runtime', format: 'jco-esm', derived_from_sha256: hash(canonical), entry: files.find(item => item.path.endsWith(entryName)),
    files, derivation_attestation: { path: 'web-runtime/' + attestationName, media_type: 'application/json', sha256: hash(attestationBytes), size_bytes: attestationBytes.length } };
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  process.stdout.write(canonicalJson(await deriveProductBrowser(resolve(process.argv[2]), resolve(process.argv[3]))) + '\n');
}
