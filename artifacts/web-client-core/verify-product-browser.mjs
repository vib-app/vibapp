// Independent verifier executable: recompute from canonical bytes before execute.
import { readFile, mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { deriveProductBrowser } from './derive-product-browser.mjs';
import { canonicalJson } from './runtime-integrity.mjs';
const hash = data => createHash('sha256').update(data).digest('hex');
function requireValue(value, reason) { if (!value) throw new Error('browser-verify:' + reason); }
export async function verifyProductBrowser(packageRoot, { timedClock = false } = {}) {
  const manifest = JSON.parse(await readFile(join(packageRoot, 'manifest.json')));
  const derivation = manifest.artifacts.browser_derivations[0];
  const scratch = await mkdtemp(join(tmpdir(), 'vibapp-browser-verifier-'));
  try {
    const rederived = await deriveProductBrowser(join(packageRoot, 'component.wasm'), scratch);
    requireValue(canonicalJson(derivation) === canonicalJson(rederived), 'derivation-not-reproducible');
    for (const file of [...derivation.files, derivation.derivation_attestation]) {
      const bytes = await readFile(join(packageRoot, file.path));
      requireValue(bytes.length === file.size_bytes && hash(bytes) === file.sha256, 'artifact-digest');
      requireValue(bytes.equals(await readFile(join(scratch, file.path))), 'artifact-rederivation');
    }
    const child = spawnSync(process.execPath, ['--max-old-space-size=256', new URL('./verify-product-browser.mjs', import.meta.url).pathname,
      '_execute', join(scratch, derivation.entry.path), JSON.stringify(manifest.app), JSON.stringify(manifest.entrypoints), String(timedClock)],
      { encoding: 'utf8', maxBuffer: 65536, timeout: 30000, env: { PATH: process.env.PATH, TZ: 'UTC' } });
    requireValue(child.status === 0, 'guest-check:' + String(child.stderr || child.stdout).slice(0, 500));
    return { schema_version: 'vibapp.product-browser-verification.v1', policy: 'vibapp.product-browser.stateless-v1',
      app_id: manifest.app.id, component_sha256: manifest.artifacts.canonical_component.sha256,
      derivation_sha256: derivation.derivation_attestation.sha256, independent_rederivation: true,
      execution: JSON.parse(child.stdout) };
  } finally { await rm(scratch, { recursive: true, force: true }); }
}
async function execute(entry, app, entrypoints, timedClock) {
  const { guest } = await import(pathToFileURL(entry));
  const descriptor = guest.describe();
  requireValue(descriptor.id === app.id && descriptor.version === app.version && descriptor.kind === 'ui'
    && descriptor.displayName === app.display_name, 'guest-identity');
  const launcher = entrypoints.find(item => item.kind === 'launcher-ui');
  requireValue(launcher && descriptor.entrypoints.length === entrypoints.length
    && descriptor.entrypoints.every(item => entrypoints.some(row => row.id === item.id && row.kind === item.kind
      && row.label === item.label && row.routes?.initial === item.initialRoute)), 'guest-entrypoints');
  requireValue(guest.getSettingsSchema() === undefined, 'stateless-profile-no-settings');
  const context = () => ({ eventId: 'verify-event', idempotencyKey: 'verify-idempotency', cancellation: 'verify-cancel',
    generation: 'verify-generation', profile: 'web-runtime', deadlineMonotonicMs: BigInt(Math.floor(performance.now() + 2000)) });
  const render = () => {
    const output = guest.handleEvent(context(), { tag: 'launcher', val: { tag: 'open', val: {
      entrypoint: launcher.id, session: 'verification-session', surface: 'surface-main', route: launcher.routes.initial, reason: 'restore' } } });
    requireValue(output.surfaces?.length === 1 && output.surfaces[0].session === 'verification-session', 'guest-surface');
    requireValue(output.surfaces[0].view?.nodes?.every(node => ['text', 'list-container'].includes(node.kind?.tag)), 'stateless-profile-read-only-view');
    requireValue(JSON.stringify(output).length < 131072, 'output-limit');
    return output.surfaces[0].view.nodes.filter(node => node.kind.tag === 'text').map(node => node.kind.val.text).join(' ');
  };
  const first = render();
  // 10,000 refreshes would exhaust the original bump allocator if instances leaked.
  for (let index = 0; index < 10000; index++) render();
  let last = render();
  if (timedClock) {
    await new Promise(resolve => setTimeout(resolve, 1200));
    last = render();
    const now = new Date().toISOString().slice(11, 19);
    requireValue(first.normalize('NFKC') !== last.normalize('NFKC') && last.normalize('NFKC') === now, 'clock-not-current-or-ticking');
  }
  return { guest_identity: true, first_text: first, last_text: last, advancing_clock: timedClock,
    refresh_count: 10003, memory_policy: 'fresh-instance-per-call' };
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const result = process.argv[2] === '_execute'
    ? await execute(process.argv[3], JSON.parse(process.argv[4]), JSON.parse(process.argv[5]), process.argv[6] === 'true')
    : await verifyProductBrowser(resolve(process.argv[2]), { timedClock: process.argv.includes('--timed-clock') });
  process.stdout.write(canonicalJson(result) + '\n');
}
