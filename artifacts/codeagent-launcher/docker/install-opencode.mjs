// Reviewed replacement for npm postinstall: select only the already-installed
// Linux/glibc package. No subprocess, download, fallback, or global configuration.
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';

const root = '/usr/local/lib/node_modules/opencode-ai';
const require = createRequire(root + '/package.json');
if (process.platform !== 'linux' || !['arm64', 'x64'].includes(process.arch)) throw new Error('unsupported image platform');
const wrapper = JSON.parse(fs.readFileSync(root + '/package.json'));
const packageName = `opencode-linux-${process.arch}`;
const packagePath = require.resolve(`${packageName}/package.json`);
const native = JSON.parse(fs.readFileSync(packagePath));
if (wrapper.version !== '1.18.27' || native.version !== wrapper.version || wrapper.optionalDependencies[packageName] !== wrapper.version) throw new Error('OpenCode version mismatch');
const source = path.join(path.dirname(packagePath), 'bin/opencode');
const bytes = fs.readFileSync(source);
if (bytes.subarray(0, 4).toString('hex') !== '7f454c46') throw new Error('expected Linux ELF executable');
const target = root + '/bin/opencode.exe';
fs.copyFileSync(source, target);
fs.chmodSync(target, 0o755);
fs.writeFileSync(root + '/vibapp-install.json', JSON.stringify({
  schema_version: 'vibapp.opencode-image-install.v1', version: native.version, native_package: packageName,
  native_sha256: createHash('sha256').update(bytes).digest('hex'),
  package_scripts_executed: false, fallback_downloads: false,
}));
