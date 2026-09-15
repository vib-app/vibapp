// Reproducible local delta to the reviewed WebTorrent 3.0.16 browser ESM only.
// The external RoomHash/node_modules source is always read-only.
import { createHash } from 'node:crypto';
import { lstat, readFile, rename, unlink, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export const UPSTREAM_SHA256 = '50bc3160dadadac2306a41908aeec90335b6fad93847b55eb23b7ef906e55c5a';
export const PATCHED_SHA256 = '1de6dcc5cba63ab802a02c4d1fa024f5768c8af47fa4a4dd96076aae5e4c098e';
export const UPSTREAM_SIZE = 239513;
export const PATCHED_SIZE = 239430;
export const ORIGINAL_HEADERS = 'headers:{"Cache-Control":"no-store","user-agent":`WebTorrent/${yi} (https://webtorrent.io)`,range:`bytes=${n}-${s}`}';
export const PATCHED_HEADERS = 'headers:{range:`bytes=${n}-${s}`}';
const OPTIONS_PREFIX = 'Re(r,{cache:"no-store",method:"GET",';
const OPTIONS_SUFFIX = ',signal:AbortSignal.timeout(6e4)})';
const PATCH_ID = 'webtorrent-3.0.16-browser-webseed-cors-headers-v1';
const NPM_INTEGRITY = 'sha512-oryVor+AGWiNbI4zBT58CKw5MrUYz2GxP5fleHyIhucqwTYAl6pjGkDTjCPGiY5VZCPnVhhhlt+eFov85ZgZKg==';
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');

export function patchWebseedHeaders(input) {
  if (!Buffer.isBuffer(input) || input.length !== UPSTREAM_SIZE || sha256(input) !== UPSTREAM_SHA256) {
    throw new Error('WebTorrent upstream bundle hash/size mismatch');
  }
  const source = input.toString('utf8');
  const original = OPTIONS_PREFIX + ORIGINAL_HEADERS + OPTIONS_SUFFIX;
  const patched = OPTIONS_PREFIX + PATCHED_HEADERS + OPTIONS_SUFFIX;
  if (source.split(ORIGINAL_HEADERS).length !== 2 || source.split(original).length !== 2) {
    throw new Error('WebTorrent webseed patch requires one exact original fetch option block');
  }
  const output = Buffer.from(source.replace(original, patched));
  if (output.length !== PATCHED_SIZE || sha256(output) !== PATCHED_SHA256) {
    throw new Error('WebTorrent patched bundle hash/size mismatch');
  }
  return output;
}

async function regularBytes(path, maximum) {
  const info = await lstat(path);
  if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1 || info.size > maximum) {
    throw new Error('WebTorrent patch refuses unsafe input/output file');
  }
  return readFile(path);
}

async function replaceRegular(path, bytes) {
  const temporary = `${path}.patch-${process.pid}`;
  try {
    await writeFile(temporary, bytes, { flag: 'wx', mode: 0o644 });
    await rename(temporary, path);
  } finally {
    await unlink(temporary).catch(error => {
      if (error.code !== 'ENOENT') throw error;
    });
  }
}

export async function applyVendorPatch({
  source = fileURLToPath(new URL('../../../RoomHash/roomhash.github.io/node_modules/webtorrent/dist/webtorrent.min.js', import.meta.url)),
  vendor = fileURLToPath(new URL('./vendor-roomhash/', import.meta.url)),
} = {}) {
  const vendorInfo = await lstat(vendor);
  if (!vendorInfo.isDirectory() || vendorInfo.isSymbolicLink()) throw new Error('WebTorrent vendor directory is unsafe');
  const input = await regularBytes(source, UPSTREAM_SIZE);
  const output = patchWebseedHeaders(input);
  const destination = join(vendor, 'webtorrent.min.js');
  const current = await regularBytes(destination, UPSTREAM_SIZE);
  if (![UPSTREAM_SHA256, PATCHED_SHA256].includes(sha256(current))) {
    throw new Error('WebTorrent destination has unreviewed local changes');
  }
  const provenancePath = join(vendor, 'PROVENANCE.json');
  const provenance = JSON.parse((await regularBytes(provenancePath, 65536)).toString('utf8'));
  if (provenance.schema_version !== 'vibapp.vendored-browser-dependency.experimental-v1'
    || provenance.package !== 'webtorrent' || provenance.version !== '3.0.16' || provenance.license !== 'MIT'
    || provenance.upstream_repository !== 'https://github.com/webtorrent/webtorrent'
    || provenance.resolved_package !== 'https://registry.npmmirror.com/webtorrent/-/webtorrent-3.0.16.tgz'
    || provenance.npm_integrity !== NPM_INTEGRITY
    || provenance.bundle?.path !== 'webtorrent.min.js' || provenance.bundle?.format !== 'esm-browser'
    || !((provenance.bundle?.sha256 === UPSTREAM_SHA256 && provenance.bundle?.size_bytes === UPSTREAM_SIZE)
      || (provenance.bundle?.sha256 === PATCHED_SHA256 && provenance.bundle?.size_bytes === PATCHED_SIZE))
    || (provenance.local_patches && (provenance.local_patches.length !== 1 || provenance.local_patches[0]?.id !== PATCH_ID))) {
    throw new Error('WebTorrent upstream provenance or local patch identity is invalid');
  }
  const upstreamBundle = {
    path: 'dist/webtorrent.min.js', format: 'esm-browser', size_bytes: UPSTREAM_SIZE, sha256: UPSTREAM_SHA256,
  };
  if (provenance.upstream_bundle && JSON.stringify(provenance.upstream_bundle) !== JSON.stringify(upstreamBundle)) {
    throw new Error('WebTorrent retained upstream bundle proof changed');
  }
  provenance.upstream_bundle = upstreamBundle;
  provenance.bundle = { path: 'webtorrent.min.js', format: 'esm-browser', size_bytes: PATCHED_SIZE, sha256: PATCHED_SHA256 };
  provenance.local_patches = [{
    id: PATCH_ID,
    scope: 'vendored-browser-esm-webseed-fetch-only',
    script: 'artifacts/web-client-core/patch-roomhash-webseed-headers.mjs',
    script_sha256: sha256(await readFile(fileURLToPath(import.meta.url))),
    input_sha256: UPSTREAM_SHA256,
    output_sha256: PATCHED_SHA256,
    occurrence_count: 1,
    removed_request_headers: ['Cache-Control', 'user-agent'],
    preserved_request_options: ['cache:no-store', 'method:GET', 'range:bytes=${n}-${s}', 'signal:AbortSignal.timeout(60000)'],
    reason: 'Avoid non-safelisted explicit browser request headers that cause GitHub Raw webseed CORS preflight failure; preserve cache policy, byte range and cancellation.',
  }];
  // No network, rebuild, package resolution or modification of Node WebTorrent.
  // A partially interrupted update fails the existing sync provenance check.
  if (!current.equals(output)) await replaceRegular(destination, output);
  const provenanceBytes = Buffer.from(JSON.stringify(provenance, null, 2) + '\n');
  if (!(await readFile(provenancePath)).equals(provenanceBytes)) await replaceRegular(provenancePath, provenanceBytes);
  return { bundle_sha256: PATCHED_SHA256, upstream_sha256: UPSTREAM_SHA256, size_bytes: PATCHED_SIZE };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2);
  if (args.length) throw new Error('Run this reviewed patch without arguments; it uses fixed local source and vendor paths');
  process.stdout.write(JSON.stringify(await applyVendorPatch()) + '\n');
}
