// Integrate already-verified release assets into the product Registry sources.
// This deliberately does not modify a Website/Sites checkout or publish a Site.
import { readFile, writeFile, mkdir, lstat, rename } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { join, dirname, resolve, isAbsolute } from 'node:path';
import { pathToFileURL } from 'node:url';
import { verifyPublicRegistryDataRoot } from './sync-public-package-locators.mjs';
import { canonicalJson, verifyBrowserPreviewBinding, verifyDerivationAttestation, verifyDerivationFileSet } from './runtime-integrity.mjs';
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const json = async path => JSON.parse(await readFile(path));
const requireValue = (value, why) => { if (!value) throw new Error('browser-integrate:' + why); };

export async function integrateBrowserRelease(projectionRoot, registryRoot, retainedRoot) {
  requireValue([projectionRoot, registryRoot, retainedRoot].every(isAbsolute), 'absolute-roots');
  await verifyPublicRegistryDataRoot({ targetRoot: join(projectionRoot, 'public/data'), productionOnly: true });
  const projection = await json(join(projectionRoot, 'public/data/registry.snapshot.json'));
  const snapshotPath = join(registryRoot, 'snapshots/registry.snapshot.json');
  const priorBytes = await readFile(snapshotPath);
  const prior = JSON.parse(priorBytes);
  requireValue(Array.isArray(prior.records) && Array.isArray(projection.records) && projection.records.length > 0, 'registry-shape');
  const additions = new Set(projection.records.map(record => record.app.id));
  requireValue(additions.size === projection.records.length, 'duplicate-app');
  for (const record of projection.records) {
    const bindings = projection.browser_artifact_bindings.filter(binding => binding.app_id === record.app.id);
    requireValue(bindings.length === 1, 'binding-count');
    const binding = bindings[0];
    await verifyBrowserPreviewBinding(record, binding);
    const files = new Map();
    for (const descriptor of [...binding.files, binding.attestation.artifact]) {
      const relative = descriptor.path.slice('/launcher/components/'.length);
      const source = join(projectionRoot, 'published-components', relative);
      const metadata = await lstat(source);
      const bytes = await readFile(source);
      requireValue(metadata.isFile() && !metadata.isSymbolicLink() && bytes.length === descriptor.size_bytes
        && hash(bytes) === descriptor.sha256, 'asset-integrity');
      files.set(descriptor.path, bytes);
    }
    await verifyDerivationAttestation(binding, files.get(binding.attestation.artifact.path));
    await verifyDerivationFileSet(binding, new Map(binding.files.map(file => [file.path, files.get(file.path)])));
    for (const [path, bytes] of files) {
      const target = join(retainedRoot, path.slice('/launcher/components/'.length));
      await mkdir(dirname(target), { recursive: true });
      try {
        const metadata = await lstat(target);
        requireValue(metadata.isFile() && !metadata.isSymbolicLink() && (await readFile(target)).equals(bytes), 'immutable-asset-conflict');
      } catch (error) {
        if (error.code !== 'ENOENT') throw error;
        await writeFile(target, bytes, { flag: 'wx' });
      }
    }
    const filename = record.package.package_digest_sha256 + '.json';
    const locator = await readFile(join(projectionRoot, 'package-locators', filename));
    const validatedLocator = await readFile(join(projectionRoot, 'public/data/package-locators', filename));
    requireValue(canonicalJson(JSON.parse(locator)) === canonicalJson(JSON.parse(validatedLocator)), 'locator-projection');
    await mkdir(join(registryRoot, 'package-locators'), { recursive: true });
    await writeFile(join(registryRoot, 'package-locators', filename), locator);
  }
  const merged = { ...prior, generated_at_utc: projection.generated_at_utc,
    records: [...prior.records.filter(record => !additions.has(record.app?.id)), ...projection.records],
    browser_artifact_bindings: [...(prior.browser_artifact_bindings || []).filter(binding => !additions.has(binding.app_id)), ...projection.browser_artifact_bindings] };
  // Asset/locator bytes are in place before the source snapshot selects them.
  requireValue((await readFile(snapshotPath)).equals(priorBytes), 'concurrent-registry-change');
  const temporary = snapshotPath + '.browser-release-' + process.pid;
  await writeFile(temporary, JSON.stringify(merged, null, 2) + '\n', { flag: 'wx' });
  await rename(temporary, snapshotPath);
  return { app_ids: [...additions], retained_prior_records: prior.records.filter(record => !additions.has(record.app?.id)).length,
    registry_snapshot: snapshotPath, retained_assets: retainedRoot, website_sync: 'required-by-site-owner' };
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  console.log(JSON.stringify(await integrateBrowserRelease(resolve(process.argv[2]), resolve(process.argv[3]), resolve(process.argv[4]))));
}
