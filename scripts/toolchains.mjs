import { existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { isAbsolute, join } from 'node:path';

// Resolve an already installed, exact toolchain without invoking rustup (which
// can download or mutate toolchains). Explicit paths support nonstandard hosts.
export function cargoPath(version, override) {
  if (override) {
    if (!isAbsolute(override)) throw new Error('Cargo override must be absolute');
    return override;
  }
  const arch = { arm64: 'aarch64', x64: 'x86_64' }[process.arch];
  const host = { darwin: 'apple-darwin', linux: 'unknown-linux-gnu', win32: 'pc-windows-msvc' }[process.platform];
  if (!arch || !host) throw new Error('Set an explicit installed Cargo path on this host');
  const candidate = join(homedir(), '.rustup', 'toolchains', `${version}-${arch}-${host}`, 'bin', process.platform === 'win32' ? 'cargo.exe' : 'cargo');
  if (!existsSync(candidate)) throw new Error(`Install Rust ${version} separately or set an explicit Cargo path`);
  return candidate;
}
