import { createHash } from 'node:crypto';
import { copyFile, mkdir, readFile, stat, writeFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { resolve, join, isAbsolute } from 'node:path';

const root = import.meta.dirname;
const input = process.argv[2];
if (!input || !isAbsolute(input) || !input.endsWith('.apk')) throw new Error('Pass the exact absolute unsigned APK path');
const sdk = process.env.ANDROID_HOME;
if (!sdk || !isAbsolute(sdk)) throw new Error('ANDROID_HOME must be an absolute Android SDK path');
const tools = join(sdk, 'build-tools', process.env.VIBAPP_ANDROID_BUILD_TOOLS || '35.0.0');
const keyRoot = process.env.VIBAPP_ANDROID_SIGNING_ROOT || resolve(root, '../../generated/client-signing/android-preview');
if (!isAbsolute(keyRoot)) throw new Error('VIBAPP_ANDROID_SIGNING_ROOT must be an absolute private signing directory');
const password = (await readFile(join(keyRoot, 'vibapp-preview.password'), 'utf8')).trim();
const output = join(root, 'output');
await mkdir(output, { recursive: true });
const aligned = join(output, 'VibApp-Android-ARM64.aligned.apk');
const artifact = join(output, 'VibApp-Android-ARM64.apk');
function run(file, args, env = process.env) {
  const result = spawnSync(file, args, { env, encoding: 'utf8', timeout: 120000, maxBuffer: 1024 * 1024 });
  if (result.status !== 0) throw new Error(`Android packaging tool failed: ${file}`);
  return result.stdout;
}
run(join(tools, 'zipalign'), ['-f', '-P', '16', '4', input, aligned]);
await copyFile(aligned, artifact);
run(join(tools, 'apksigner'), ['sign', '--ks', join(keyRoot, 'vibapp-preview.jks'), '--ks-key-alias', 'vibapp-preview',
  '--ks-pass', 'env:VIBAPP_SIGNING_PASSWORD', '--key-pass', 'env:VIBAPP_SIGNING_PASSWORD', artifact],
{ ...process.env, VIBAPP_SIGNING_PASSWORD: password });
const verification = run(join(tools, 'apksigner'), ['verify', '--verbose', '--print-certs', artifact]);
run(join(tools, 'zipalign'), ['-c', '-P', '16', '4', artifact]);
const sha256 = createHash('sha256').update(await readFile(artifact)).digest('hex');
await writeFile(join(output, 'SHA256SUMS'), `${sha256}  VibApp-Android-ARM64.apk\n`);
await writeFile(join(output, 'android-signature-verification.txt'), verification);
await writeFile(join(output, 'android-build-receipt.json'), JSON.stringify({
  schema_version: 'vibapp.android-preview-build.v1', platform: 'android-arm64',
  artifact: 'VibApp-Android-ARM64.apk', sha256, size_bytes: (await stat(artifact)).size,
  signing: 'stable-local-preview-key-not-google-play',
  execution_profile: 'web-runtime', background: 'foreground-only',
  source_commit: process.env.VIBAPP_RELEASE_SOURCE_COMMIT || null,
  source_commit_verified: false,
}, null, 2) + '\n');
console.log(`Signed and verified: ${artifact}\nSHA256 ${sha256}`);
