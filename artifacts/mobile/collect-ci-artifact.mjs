import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { copyFile, mkdir, readFile, stat, writeFile } from 'node:fs/promises';
import { resolve, join } from 'node:path';

if (process.env.GITHUB_ACTIONS !== 'true' || !/^[0-9a-f]{40}$/.test(process.env.GITHUB_SHA || '')) {
  throw new Error('This collector requires an exact GitHub Actions source SHA');
}
const root = import.meta.dirname;
const java = spawnSync(join(process.env.JAVA_HOME || '', 'bin/java'), ['-version'], {
  encoding: 'utf8', timeout: 10000, maxBuffer: 16384,
});
const javaVersion = `${java.stdout || ''}${java.stderr || ''}`.trim();
if (java.status !== 0 || !javaVersion.includes('21.0.9+10')) {
  throw new Error('CI Java runtime differs from the pinned Temurin 21.0.9+10');
}
const input = join(root, 'src-tauri/gen/android/app/build/outputs/apk/universal/release/app-universal-release-unsigned.apk');
const output = resolve(root, '../../generated/client-release-android');
const name = 'VibApp-Android-ARM64-unsigned.apk';
const size = (await stat(input)).size;
if (size < 1000000 || size > 128 * 1024 * 1024) throw new Error('APK is outside the bounded release size');
const bytes = await readFile(input);
if (bytes[0] !== 0x50 || bytes[1] !== 0x4b) throw new Error('APK is not a ZIP container');
const sha256 = createHash('sha256').update(bytes).digest('hex');
await mkdir(output, { recursive: true });
await copyFile(input, join(output, name));
await writeFile(join(output, 'SHA256SUMS-android-unsigned'), `${sha256}  ${name}\n`);
await writeFile(join(output, 'android-build-receipt.json'), JSON.stringify({
  schema_version: 'vibapp.android-ci-build.v1', source_commit: process.env.GITHUB_SHA,
  artifact: name, sha256, size_bytes: size, platform: 'android-arm64',
  signing: 'unsigned-do-not-distribute', runtime_smoke_performed: false,
  tools: { rust: '1.98.0', web_core_rust: '1.93.0', ndk: '28.2.13676358', sdk: 36,
    java: '21.0.9+10', java_version_output: javaVersion, tauri_cli: '2.11.4', tauri: '2.11.5' },
  profile: 'web-runtime', background: 'foreground-only',
}, null, 2) + '\n');
console.log(`Collected unsigned ARM64 APK from ${process.env.GITHUB_SHA}: ${sha256}`);
