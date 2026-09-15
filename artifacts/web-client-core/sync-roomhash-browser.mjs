import { createHash } from 'node:crypto';
import { cp, mkdir, open, readFile, readdir, rm, stat, unlink, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(fileURLToPath(import.meta.url));
const artifactsRoot = dirname(root);
const sourceAdapter = join(root, 'roomhash-browser-node.mjs');
const sourcePackageStore = join(root, 'browser-package-store.mjs');
const sourceVendor = join(root, 'vendor-roomhash');
const sourceCollaboration = join(artifactsRoot, 'roomhash-collaboration', 'src', 'index.mjs');
const sourceBrowserFactory = join(artifactsRoot, 'roomhash-collaboration', 'src', 'browser-roomhash-factory.mjs');
const sourceBrowserBroker = join(artifactsRoot, 'roomhash-collaboration', 'src', 'browser-broker.mjs');
const roomHashWebsite = process.env.VIBAPP_ROOMHASH_WEB_ROOT
  ?? join(root, '..', '..', '..', 'RoomHash', 'roomhash.github.io');
const trysteroRoot = join(roomHashWebsite, 'node_modules', '@trystero-p2p');
const torrentRoot = join(trysteroRoot, 'torrent');
const coreRoot = join(trysteroRoot, 'core');
const sourceTorrentEntry = join(torrentRoot, 'dist', 'index.mjs');
const sourceCoreDist = join(coreRoot, 'dist');
const target = join(artifactsRoot, 'product-platform', 'website', 'public', 'roomhash');
const targetVendor = join(target, 'vendor-roomhash');
const targetCore = join(targetVendor, 'trystero-core');
const syncLock = join(dirname(target), '.roomhash-browser-sync.lock');

const TRYSTERO_VERSION = '0.25.3';
const MIT_LICENSE_SHA256 = 'bbf91fd979faac0def9551c570e2e9f92b4e02d22f38ca5d98860b6284e1ea25';
const TORRENT_ENTRY_SHA256 = 'ea3835d800f575cca18fad9423e5da687c21f4e0034fdb41526367d0063cc70d';
const BARE_CORE_IMPORT = 'from "@trystero-p2p/core";';
const RELATIVE_CORE_IMPORT = 'from "./trystero-core/index.mjs";';
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');

async function acquireSyncLock() {
  const deadline = Date.now() + 10_000;
  for (;;) {
    try {
      const handle = await open(syncLock, 'wx', 0o600);
      await handle.writeFile(`${process.pid}\n`, 'utf8');
      return async () => {
        await handle.close().catch(() => {});
        await unlink(syncLock).catch(() => {});
      };
    } catch (error) {
      if (error?.code !== 'EEXIST') throw error;
      const age = Date.now() - (await stat(syncLock).catch(() => ({ mtimeMs: Date.now() }))).mtimeMs;
      if (age > 120_000) {
        await unlink(syncLock).catch(() => {});
        continue;
      }
      if (Date.now() >= deadline) throw new Error('timed out waiting for RoomHash browser sync lock');
      await new Promise(resolve => setTimeout(resolve, 25));
    }
  }
}

async function validateTrysteroPackage(packageRoot, expectedName) {
  const packageBytes = await readFile(join(packageRoot, 'package.json'));
  const metadata = JSON.parse(packageBytes.toString('utf8'));
  const license = await readFile(join(packageRoot, 'LICENSE'));
  if (metadata.name !== expectedName || metadata.version !== TRYSTERO_VERSION || metadata.license !== 'MIT') {
    throw new Error(`${expectedName} must be the reviewed MIT-licensed ${TRYSTERO_VERSION} package`);
  }
  if (
    sha256(license) !== MIT_LICENSE_SHA256
    || !license.toString('utf8').includes('Permission is hereby granted, free of charge')
  ) {
    throw new Error(`${expectedName} failed MIT license verification`);
  }
  return { license, metadata, packageBytes };
}

async function fileHashes(directory, prefix = '') {
  const hashes = {};
  const entries = await readdir(directory, { withFileTypes: true });
  for (const entry of entries.sort((left, right) => left.name.localeCompare(right.name))) {
    const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (relative === 'SYNC.json') continue;
    const absolute = join(directory, entry.name);
    if (entry.isDirectory()) {
      Object.assign(hashes, await fileHashes(absolute, relative));
    } else if (entry.isFile()) {
      hashes[relative] = sha256(await readFile(absolute));
    } else {
      throw new Error(`RoomHash browser sync refuses non-file entry: ${relative}`);
    }
  }
  return hashes;
}

const webTorrentProvenance = JSON.parse(await readFile(join(sourceVendor, 'PROVENANCE.json'), 'utf8'));
if (
  webTorrentProvenance.schema_version !== 'vibapp.vendored-browser-dependency.experimental-v1'
  || webTorrentProvenance.package !== 'webtorrent'
  || webTorrentProvenance.version !== '3.0.16'
  || webTorrentProvenance.license !== 'MIT'
) throw new Error('RoomHash browser vendor provenance is invalid');
const bundle = await readFile(join(sourceVendor, 'webtorrent.min.js'));
if (
  bundle.byteLength !== webTorrentProvenance.bundle.size_bytes
  || sha256(bundle) !== webTorrentProvenance.bundle.sha256
) {
  throw new Error('RoomHash WebTorrent browser bundle failed provenance verification');
}
if (!bundle.subarray(Math.max(0, bundle.length - 256)).toString('utf8').includes('export{Qi as default}')) {
  throw new Error('RoomHash WebTorrent browser bundle is not the reviewed ESM build');
}

const torrentPackage = await validateTrysteroPackage(torrentRoot, '@trystero-p2p/torrent');
const corePackage = await validateTrysteroPackage(coreRoot, '@trystero-p2p/core');
if (torrentPackage.metadata.dependencies?.['@trystero-p2p/core'] !== TRYSTERO_VERSION) {
  throw new Error('@trystero-p2p/torrent must depend on the exact reviewed core version');
}
const torrentEntry = await readFile(sourceTorrentEntry, 'utf8');
if (sha256(torrentEntry) !== TORRENT_ENTRY_SHA256) {
  throw new Error('@trystero-p2p/torrent entry failed reviewed source verification');
}
if (torrentEntry.split(BARE_CORE_IMPORT).length !== 2) {
  throw new Error('@trystero-p2p/torrent must contain exactly one reviewed bare core import');
}
const rewrittenTorrentEntry = torrentEntry.replace(BARE_CORE_IMPORT, RELATIVE_CORE_IMPORT);
if (rewrittenTorrentEntry.includes('@trystero-p2p/core')) {
  throw new Error('@trystero-p2p/torrent bare core import rewrite was incomplete');
}

const adapter = await readFile(sourceAdapter);
const packageStore = await readFile(sourcePackageStore);
const collaboration = await readFile(sourceCollaboration);
const browserFactory = await readFile(sourceBrowserFactory);
const browserBroker = await readFile(sourceBrowserBroker);
const coreDistHashes = await fileHashes(sourceCoreDist);
const trysteroProvenance = {
  schema_version: 'vibapp.roomhash-trystero-provenance.experimental-v1',
  source_project: 'RoomHash/roomhash.github.io/node_modules',
  packages: {
    '@trystero-p2p/core': {
      version: TRYSTERO_VERSION,
      license: 'MIT',
      package_sha256: sha256(corePackage.packageBytes),
      license_sha256: sha256(corePackage.license),
      dist_files: coreDistHashes,
    },
    '@trystero-p2p/torrent': {
      version: TRYSTERO_VERSION,
      license: 'MIT',
      package_sha256: sha256(torrentPackage.packageBytes),
      license_sha256: sha256(torrentPackage.license),
      source_entry_sha256: sha256(torrentEntry),
      synced_entry_sha256: sha256(rewrittenTorrentEntry),
    },
  },
  rewrite: {
    source: BARE_CORE_IMPORT,
    replacement: RELATIVE_CORE_IMPORT,
    occurrence_count: 1,
  },
};
const trysteroProvenanceBytes = Buffer.from(JSON.stringify(trysteroProvenance, null, 2) + '\n');
const sourceVendorHashes = await fileHashes(sourceVendor);
const torrentMap = await readFile(join(torrentRoot, 'dist', 'index.mjs.map'));
const desiredFiles = {
  'roomhash-browser-node.mjs': sha256(adapter),
  'browser-package-store.mjs': sha256(packageStore),
  'index.mjs': sha256(collaboration),
  'browser-roomhash-factory.mjs': sha256(browserFactory),
  'browser-broker.mjs': sha256(browserBroker),
  ...Object.fromEntries(Object.entries(sourceVendorHashes).map(([name, digest]) => [`vendor-roomhash/${name}`, digest])),
  ...Object.fromEntries(Object.entries(coreDistHashes).map(([name, digest]) => [`vendor-roomhash/trystero-core/${name}`, digest])),
  'vendor-roomhash/trystero-core/LICENSE': sha256(corePackage.license),
  'vendor-roomhash/trystero-torrent.LICENSE': sha256(torrentPackage.license),
  'vendor-roomhash/trystero-torrent.mjs': sha256(rewrittenTorrentEntry),
  'vendor-roomhash/index.mjs.map': sha256(torrentMap),
  'vendor-roomhash/TRYSTERO-PROVENANCE.json': sha256(trysteroProvenanceBytes),
};

async function targetIsCurrent() {
  try {
    const manifest = JSON.parse(await readFile(join(target, 'SYNC.json'), 'utf8'));
    const observed = await fileHashes(target);
    const expectedNames = Object.keys(desiredFiles).sort();
    const observedNames = Object.keys(observed).sort();
    return manifest.schema_version === 'vibapp.roomhash-browser-sync.experimental-v1'
      && JSON.stringify(expectedNames) === JSON.stringify(observedNames)
      && expectedNames.every(name => observed[name] === desiredFiles[name] && manifest.files?.[name] === desiredFiles[name])
      && manifest.adapter_sha256 === desiredFiles['roomhash-browser-node.mjs']
      && manifest.package_store_sha256 === desiredFiles['browser-package-store.mjs']
      && manifest.bundle_sha256 === desiredFiles['vendor-roomhash/webtorrent.min.js']
      && manifest.collaboration_adapter_sha256 === desiredFiles['index.mjs']
      && manifest.browser_collaboration_factory_sha256 === desiredFiles['browser-roomhash-factory.mjs']
      && manifest.browser_collaboration_broker_sha256 === desiredFiles['browser-broker.mjs']
      && manifest.trystero_provenance_sha256 === desiredFiles['vendor-roomhash/TRYSTERO-PROVENANCE.json'];
  } catch {
    return false;
  }
}

const releaseSyncLock = await acquireSyncLock();
try {
  if (await targetIsCurrent()) {
    process.stdout.write(target + '\n');
  } else {
  await rm(target, { recursive: true, force: true });
  await mkdir(target, { recursive: true });
  await cp(sourceAdapter, join(target, 'roomhash-browser-node.mjs'), { force: false, errorOnExist: true });
  await cp(sourcePackageStore, join(target, 'browser-package-store.mjs'), { force: false, errorOnExist: true });
  await cp(sourceCollaboration, join(target, 'index.mjs'), { force: false, errorOnExist: true });
  await cp(sourceBrowserFactory, join(target, 'browser-roomhash-factory.mjs'), { force: false, errorOnExist: true });
  await cp(sourceBrowserBroker, join(target, 'browser-broker.mjs'), { force: false, errorOnExist: true });
  await cp(sourceVendor, targetVendor, { recursive: true, force: false, errorOnExist: true });
  await cp(sourceCoreDist, targetCore, { recursive: true, force: false, errorOnExist: true });
  await cp(join(coreRoot, 'LICENSE'), join(targetCore, 'LICENSE'), { force: false, errorOnExist: true });
  await cp(join(torrentRoot, 'LICENSE'), join(targetVendor, 'trystero-torrent.LICENSE'), { force: false, errorOnExist: true });
  await writeFile(join(targetVendor, 'trystero-torrent.mjs'), rewrittenTorrentEntry, 'utf8');
  await cp(
    join(torrentRoot, 'dist', 'index.mjs.map'),
    join(targetVendor, 'index.mjs.map'),
    { force: false, errorOnExist: true },
  );
  await writeFile(
    join(targetVendor, 'TRYSTERO-PROVENANCE.json'),
    trysteroProvenanceBytes,
  );

  const files = await fileHashes(target);
  await writeFile(join(target, 'SYNC.json'), JSON.stringify({
    schema_version: 'vibapp.roomhash-browser-sync.experimental-v1',
    adapter_sha256: sha256(adapter),
    package_store_sha256: sha256(packageStore),
    bundle_sha256: sha256(bundle),
    collaboration_adapter_sha256: sha256(collaboration),
    browser_collaboration_factory_sha256: sha256(browserFactory),
    browser_collaboration_broker_sha256: sha256(browserBroker),
    trystero_provenance_sha256: files['vendor-roomhash/TRYSTERO-PROVENANCE.json'],
    files,
  }, null, 2) + '\n', 'utf8');
  process.stdout.write(target + '\n');
  }
} finally {
  await releaseSyncLock();
}
