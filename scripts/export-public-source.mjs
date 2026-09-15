// Initial import into the public product repository; never exports local Git history.
import { execFileSync } from 'node:child_process';
import { cp, lstat, mkdir, readFile, readdir } from 'node:fs/promises';
import { resolve, dirname, relative, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { desktopBuildInputs } from '../artifacts/desktop/scripts/desktop-build-inputs.mjs';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const destination = resolve(process.argv[2] || '');
if (!process.argv[2] || destination === root) throw new Error('Provide a new, empty export directory');
await mkdir(destination, { recursive: true });
if ((await readdir(destination)).length) throw new Error('Export destination must be empty');
const files = new Set(execFileSync('git', ['ls-files', '--cached', '--others', '--exclude-standard', '-z'],
  { cwd: root, encoding: 'utf8' }).split('\0').filter(Boolean));
for (const input of await desktopBuildInputs()) files.add(relative(root, input.path));
async function tree(path) {
  for (const entry of await readdir(join(root, path), { withFileTypes: true })) {
    if (entry.name === '__pycache__' || /\.py[co]$/.test(entry.name)) continue;
    if (entry.isDirectory()) await tree(join(path, entry.name));
    else files.add(join(path, entry.name));
  }
}
for (const path of ['artifacts/cloud-agent/starter', 'artifacts/product-platform/registry/snapshots', 'artifacts/product-platform/registry/package-locators']) await tree(path);
let bytes = 0;
for (const path of [...files].sort()) {
  if (/(^|\/)(generated|node_modules|\.git|\.chief-of-staff|\.butler|__pycache__)(\/|$)|\.(pyc|pyo|log|sqlite|db|pem|key)$/.test(path)) throw new Error(`Unsafe export path: ${path}`);
  if (/(^|\/)\.env(?:\.|$)/.test(path) && !path.endsWith('.example')) throw new Error(`Environment file excluded: ${path}`);
  const source = join(root, path);
  const stat = await lstat(source);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 16 * 1024 * 1024) throw new Error(`Unexpected export input: ${path}`);
  const content = await readFile(source);
  // Print paths only, never matched secret values. Synthetic tests deliberately
  // containing a PEM marker but no real key body do not trigger this check.
  if (/gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\s+[A-Za-z0-9+/=]{60}/.test(content.toString('utf8'))) throw new Error(`Credential-like content requires review: ${path}`);
  bytes += stat.size;
  if (bytes > 128 * 1024 * 1024) throw new Error('Public source export exceeds budget');
  const target = join(destination, path);
  await mkdir(dirname(target), { recursive: true });
  await cp(source, target, { preserveTimestamps: true });
}
console.log(JSON.stringify({ destination, files: files.size, bytes, history: 'not-exported' }));
