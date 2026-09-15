import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { readFile, readdir } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const targetUrl = new URL('../../product-platform/website/public/roomhash/', import.meta.url);
const targetPath = fileURLToPath(targetUrl);
const roomHashModules = new URL('../../../../RoomHash/roomhash.github.io/node_modules/@trystero-p2p/', import.meta.url);
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');

async function fileHashes(directory, prefix = '') {
  const hashes = {};
  const entries = await readdir(directory, { withFileTypes: true });
  for (const entry of entries.sort((left, right) => left.name.localeCompare(right.name))) {
    const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
    const absolute = join(directory, entry.name);
    if (entry.isDirectory()) Object.assign(hashes, await fileHashes(absolute, relative));
    else if (entry.isFile() && relative !== 'SYNC.json') hashes[relative] = sha256(await readFile(absolute));
    else if (!entry.isFile()) assert.fail(`unexpected generated entry: ${relative}`);
  }
  return hashes;
}

function importSpecifiers(source) {
  const result = [];
  const pattern = /\b(?:from\s+|import\s*(?:\(\s*)?)(['"])([^'"]+)\1/g;
  for (const match of source.matchAll(pattern)) result.push(match[2]);
  return result;
}

test('RoomHash browser collaboration sync is reproducible and browser-only', async () => {
  const result = spawnSync(process.execPath, [fileURLToPath(new URL('../sync-roomhash-browser.mjs', import.meta.url))], {
    encoding: 'utf8',
  });
  assert.equal(result.status, 0, result.stderr || result.stdout);

  const manifest = JSON.parse(await readFile(new URL('SYNC.json', targetUrl), 'utf8'));
  assert.equal(manifest.schema_version, 'vibapp.roomhash-browser-sync.experimental-v1');
  assert.deepEqual(manifest.files, await fileHashes(targetPath));
  assert.equal(manifest.adapter_sha256, manifest.files['roomhash-browser-node.mjs']);
  assert.equal(manifest.package_store_sha256, manifest.files['browser-package-store.mjs']);
  assert.equal(manifest.collaboration_adapter_sha256, manifest.files['index.mjs']);
  assert.equal(manifest.browser_collaboration_factory_sha256, manifest.files['browser-roomhash-factory.mjs']);
  assert.equal(manifest.browser_collaboration_broker_sha256, manifest.files['browser-broker.mjs']);
  assert.equal(manifest.bundle_sha256, manifest.files['vendor-roomhash/webtorrent.min.js']);
  assert.equal(manifest.trystero_provenance_sha256, manifest.files['vendor-roomhash/TRYSTERO-PROVENANCE.json']);

  assert.deepEqual(
    await readFile(new URL('browser-package-store.mjs', targetUrl)),
    await readFile(new URL('../browser-package-store.mjs', import.meta.url)),
  );
  assert.deepEqual(
    await readFile(new URL('index.mjs', targetUrl)),
    await readFile(new URL('../../roomhash-collaboration/src/index.mjs', import.meta.url)),
  );
  assert.deepEqual(
    await readFile(new URL('browser-roomhash-factory.mjs', targetUrl)),
    await readFile(new URL('../../roomhash-collaboration/src/browser-roomhash-factory.mjs', import.meta.url)),
  );
  assert.deepEqual(
    await readFile(new URL('browser-broker.mjs', targetUrl)),
    await readFile(new URL('../../roomhash-collaboration/src/browser-broker.mjs', import.meta.url)),
  );

  const sourceTorrent = await readFile(new URL('torrent/dist/index.mjs', roomHashModules), 'utf8');
  const generatedTorrent = await readFile(new URL('vendor-roomhash/trystero-torrent.mjs', targetUrl), 'utf8');
  assert.equal(
    generatedTorrent,
    sourceTorrent.replace('from "@trystero-p2p/core";', 'from "./trystero-core/index.mjs";'),
  );
  const coreFiles = await readdir(fileURLToPath(new URL('core/dist/', roomHashModules)));
  for (const name of coreFiles) {
    assert.deepEqual(
      await readFile(new URL(`vendor-roomhash/trystero-core/${name}`, targetUrl)),
      await readFile(new URL(`core/dist/${name}`, roomHashModules)),
      `generated Trystero core file drifted: ${name}`,
    );
  }

  const provenance = JSON.parse(await readFile(new URL('vendor-roomhash/TRYSTERO-PROVENANCE.json', targetUrl), 'utf8'));
  assert.equal(provenance.schema_version, 'vibapp.roomhash-trystero-provenance.experimental-v1');
  for (const packageName of ['@trystero-p2p/core', '@trystero-p2p/torrent']) {
    assert.equal(provenance.packages[packageName].version, '0.25.3');
    assert.equal(provenance.packages[packageName].license, 'MIT');
    assert.equal(
      provenance.packages[packageName].license_sha256,
      sha256(await readFile(new URL(`${packageName.endsWith('/core') ? 'core' : 'torrent'}/LICENSE`, roomHashModules))),
    );
  }
  assert.deepEqual(provenance.rewrite, {
    source: 'from "@trystero-p2p/core";',
    replacement: 'from "./trystero-core/index.mjs";',
    occurrence_count: 1,
  });

  for (const relative of Object.keys(manifest.files).filter(name => name.endsWith('.mjs'))) {
    const source = await readFile(new URL(relative, targetUrl), 'utf8');
    assert.doesNotMatch(source, /node:/, `${relative} imports Node authority`);
    assert.doesNotMatch(source, /MeshNode|gossip/i, `${relative} contains RoomHash mesh/gossip authority`);
    for (const specifier of importSpecifiers(source)) {
      assert.match(specifier, /^(?:\.{1,2}\/|\/|https?:\/\/|data:|blob:)/, `${relative} has bare import ${specifier}`);
    }
  }
});
