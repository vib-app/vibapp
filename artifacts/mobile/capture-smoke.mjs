import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdir, writeFile } from 'node:fs/promises';
import { isAbsolute, join } from 'node:path';

const sdk = process.env.ANDROID_HOME;
const label = process.argv[2] || 'smoke';
if (!sdk || !isAbsolute(sdk) || !/^[a-z0-9-]{1,48}$/.test(label)) throw new Error('Absolute ANDROID_HOME and a bounded evidence label are required');
const adb = join(sdk, 'platform-tools', process.platform === 'win32' ? 'adb.exe' : 'adb');
function run(args, binary = false) {
  const result = spawnSync(adb, ['-e', ...args], { timeout: 15000, maxBuffer: binary ? 8 * 1024 * 1024 : 1024 * 1024, encoding: binary ? null : 'utf8' });
  if (result.status !== 0) throw new Error(`Android emulator evidence command failed: ${args[0]}`);
  return result.stdout;
}
const pid = run(['shell', 'pidof', 'ai.vibapp.client.preview']).trim();
if (!/^\d+$/.test(pid)) throw new Error('Preview app is not running with exactly one host PID');
const png = run(['exec-out', 'screencap', '-p'], true);
const output = join(import.meta.dirname, 'output');
await mkdir(output, { recursive: true });
await writeFile(join(output, `android-${label}.png`), png);
const log = run(['logcat', '-d', '-t', '300', '--pid', pid]);
await writeFile(join(output, `android-${label}.log`), log);
await writeFile(join(output, `android-${label}.json`), JSON.stringify({
  schema_version: 'vibapp.android-smoke-evidence.v1', app_id: 'ai.vibapp.client.preview',
  observed_at_utc: new Date().toISOString(), host_pid: Number(pid),
  screenshot: `android-${label}.png`, screenshot_sha256: createHash('sha256').update(png).digest('hex'),
  app_content_acceptance: 'requires-independent-observation',
}, null, 2) + '\n');
console.log(`Captured Android emulator evidence: android-${label}`);
