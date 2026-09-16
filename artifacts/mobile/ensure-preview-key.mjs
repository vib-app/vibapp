import { randomBytes } from 'node:crypto';
import { chmod, lstat, mkdir, readFile, writeFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { resolve, join } from 'node:path';

// This creates a stable local Android *preview* identity, not a Play Store key.
// Outputs are private release credentials and must never be copied to Git.
const keyRoot = resolve(import.meta.dirname, '../../generated/client-signing/android-preview');
const keyPath = join(keyRoot, 'vibapp-preview.jks');
const passwordPath = join(keyRoot, 'vibapp-preview.password');
const javaHome = process.env.JAVA_HOME;
if (!javaHome?.startsWith('/')) throw new Error('JAVA_HOME must identify an installed JDK with keytool');
await mkdir(keyRoot, { recursive: true, mode: 0o700 });
await chmod(keyRoot, 0o700);
const exists = async path => lstat(path).then(stat => { if (!stat.isFile() || stat.isSymbolicLink()) throw new Error('Signing credential must be a regular file'); return true; }, error => { if (error.code === 'ENOENT') return false; throw error; });
const [keyExists, passwordExists] = await Promise.all([exists(keyPath), exists(passwordPath)]);
if (keyExists !== passwordExists) throw new Error('Incomplete preview signing identity; restore its matching key/password, never silently replace it');
if (!keyExists) {
  const password = randomBytes(32).toString('hex');
  await writeFile(passwordPath, password + '\n', { flag: 'wx', mode: 0o600 });
  const result = spawnSync(join(javaHome, 'bin/keytool'), ['-genkeypair', '-keystore', keyPath,
    '-storetype', 'JKS', '-alias', 'vibapp-preview', '-keyalg', 'RSA', '-keysize', '3072',
    '-validity', '3650', '-dname', 'CN=VibApp Android Preview, O=VibApp',
    '-storepass:env', 'VIBAPP_SIGNING_PASSWORD', '-keypass:env', 'VIBAPP_SIGNING_PASSWORD'],
  { env: { ...process.env, VIBAPP_SIGNING_PASSWORD: password }, encoding: 'utf8', timeout: 30_000, maxBuffer: 65536 });
  if (result.status !== 0) throw new Error('Preview signing key generation failed; inspect keytool setup without publishing credentials');
}
await chmod(keyPath, 0o600);
await chmod(passwordPath, 0o600);
if ((await readFile(passwordPath, 'utf8')).trim().length < 32) throw new Error('Preview key password file is invalid');
console.log(`Stable Android preview signing identity ready at ${keyRoot}. Keep this private directory backed up.`);
