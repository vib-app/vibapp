import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { isAbsolute, join } from 'node:path';

const root = import.meta.dirname;
const env = {};
// Tauri can render its child environment in a build failure. Never forward
// GitHub/provider/signing credentials or an SSH agent to dependency build scripts.
for (const key of ['PATH', 'HOME', 'TMPDIR', 'TEMP', 'TMP', 'SystemRoot', 'WINDIR',
  'COMSPEC', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'PROGRAMFILES',
  'JAVA_HOME', 'ANDROID_HOME', 'NDK_HOME', 'RUSTUP_TOOLCHAIN', 'RUSTUP_HOME', 'CARGO_HOME', 'CARGO_TARGET_DIR']) {
  if (process.env[key]) env[key] = process.env[key];
}
for (const key of ['JAVA_HOME', 'ANDROID_HOME', 'NDK_HOME']) {
  if (!env[key] || !isAbsolute(env[key])) throw new Error(`${key} must be an absolute installed tool path`);
}
if (!/^1\.98\.0-[a-z0-9_-]+$/.test(env.RUSTUP_TOOLCHAIN || '')) throw new Error('Set a fully qualified installed Rust 1.98.0 host toolchain');
Object.assign(env, { RUSTUP_AUTO_INSTALL: '0', CARGO_NET_OFFLINE: 'true', CARGO_BUILD_JOBS: '2',
  CARGO_INCREMENTAL: '0', LANG: 'en_US.UTF-8', TZ: 'UTC', CI: 'true' });
const lockPath = join(root, 'src-tauri/Cargo.lock');
const sha256 = value => createHash('sha256').update(value).digest('hex');
const before = sha256(await readFile(lockPath));
await mkdir(join(root, 'output'), { recursive: true });
const preparation = spawnSync(process.execPath, [join(root, 'prepare-ui.mjs')], { cwd: root, env,
  encoding: 'utf8', timeout: 60_000, maxBuffer: 1024 * 1024 });
if (preparation.status !== 0) throw new Error(`Mobile shared GUI preparation failed: ${preparation.stderr}`);
console.log(preparation.stdout.trim());
const result = spawnSync(process.platform === 'win32' ? 'npm.cmd' : 'npm',
  ['run', 'tauri', '--', 'android', 'build', '--apk', '--target', 'aarch64', '--ci'],
  { cwd: root, env, encoding: 'utf8', timeout: 30 * 60_000, maxBuffer: 4 * 1024 * 1024, detached: process.platform !== 'win32' });
if (result.error && process.platform !== 'win32' && Number.isInteger(result.pid) && result.pid > 1) {
  // The detached group belongs solely to this build, including Cargo/Gradle.
  try { process.kill(-result.pid, 'SIGKILL'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
}
await writeFile(join(root, 'output/android-build.log'), (result.stdout || '') + (result.stderr || ''));
if (before !== sha256(await readFile(lockPath))) throw new Error('Android build changed Cargo.lock; release rejected');
if (result.status !== 0) throw new Error('Android build failed; inspect output/android-build.log');
console.log('Android ARM64 unsigned APK built. Sign and independently verify before release.');
