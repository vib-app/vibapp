// Only packages a checksum-verified, trusted release dependency, never app input.
import { cp, lstat, readdir, realpath, rm } from 'node:fs/promises';
import { resolve, relative, join } from 'node:path';
import { execFileSync } from 'node:child_process';

const [sourceArg, destinationArg] = process.argv.slice(2);
if (!sourceArg || !destinationArg) throw new Error('usage: bundle-python.mjs SOURCE NEW_DESTINATION');
const source = await realpath(sourceArg);
const destination = resolve(destinationArg);
try { await lstat(destination); throw new Error('destination already exists'); }
catch (error) { if (error.code !== 'ENOENT') throw error; }
if (!(await lstat(join(source, 'bin/python3.13'))).isFile()) throw new Error('Python executable missing');
await cp(source, destination, { recursive: true, verbatimSymlinks: true });

async function visit(directory) {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isSymbolicLink()) {
      const target = await realpath(path);
      const suffix = relative(destination, target);
      if (suffix.startsWith('..') || suffix.startsWith('/')) throw new Error(`escaping Python link: ${path}`);
    } else if (entry.name === '__pycache__' || /\.py[co]$/.test(entry.name)) {
      // Only our newly created staging tree, never the source or user data.
      await rm(path, { recursive: entry.isDirectory() });
    } else if (entry.isDirectory()) {
      await visit(path);
    } else if (entry.isFile()) {
      const kind = execFileSync('/usr/bin/file', ['-b', path], { encoding: 'utf8' });
      if (kind.includes('Mach-O')) {
        if (!kind.includes('arm64')) throw new Error(`wrong Python architecture: ${path}`);
        const libraries = execFileSync('/usr/bin/otool', ['-L', path], { encoding: 'utf8' }).split('\n').slice(1);
        for (const line of libraries) {
          const dependency = line.trim().split(' ')[0];
          if (dependency && !/^(@|\/usr\/lib\/|\/System\/Library\/)/.test(dependency)) {
            throw new Error(`non-relocatable Python dependency: ${dependency}`);
          }
        }
        execFileSync('/usr/bin/codesign', ['--force', '--sign', '-', '--timestamp=none', path], { stdio: 'pipe' });
      }
    } else throw new Error(`unsupported Python file type: ${path}`);
  }
}
await visit(destination);
execFileSync(join(destination, 'bin/python3.13'), ['-I', '-B', '-c',
  'import tomllib,ssl,sqlite3,ctypes,multiprocessing;print("Bundled Python: relocation smoke passed")'],
{ env: { PATH: '/usr/bin:/bin' }, timeout: 20000, stdio: 'inherit' });
