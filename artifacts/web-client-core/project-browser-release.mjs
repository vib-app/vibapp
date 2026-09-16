// Turn an independently checked, publicly released package into same-origin
// runtime assets. External receipts are inputs, never fetched or manufactured.
import { createHash } from 'node:crypto';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { canonicalJson, bindingPayload, verifyBrowserPreviewBinding, verifyDerivationAttestation, verifyDerivationFileSet } from './runtime-integrity.mjs';
import { verifyProductBrowser } from './verify-product-browser.mjs';
import { isPublicRegistryRecord, isRealPublicBrowserBinding, syncPublicRegistryDataRoot } from './sync-public-package-locators.mjs';
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const bytes = document => Buffer.from(canonicalJson(document) + '\n');
const json = async path => JSON.parse(await readFile(path));
const requireValue = (value, why) => { if (!value) throw new Error('browser-project:' + why); };

export async function projectBrowserRelease(candidateRoot, publicationRoot, outputRoot) {
  const candidateBytes = await readFile(join(candidateRoot, 'candidate.json'));
  const candidate = JSON.parse(candidateBytes);
  const manifest = await json(join(candidateRoot, 'package/manifest.json'));
  const source = await json(join(publicationRoot, 'source-archive-receipt.json'));
  const receipt = await json(join(publicationRoot, 'release-receipt.json'));
  const listing = await json(join(publicationRoot, 'store-publication-receipt.json'));
  const digest = candidate.package_digest_sha256;
  requireValue(candidate.state === 'candidate-ready' && source.package_digest_sha256 === digest && source.organization === 'vib-app'
    && source.repository === 'sources' && source.source_digest_sha256 === candidate.source_tree_sha256, 'source-binding');
  requireValue(receipt.state === 'public-downloads-verified' && receipt.package_digest_sha256 === digest
    && receipt.binding.candidate_sha256 === hash(candidateBytes) && listing.state === 'listed' && listing.package_digest_sha256 === digest, 'release-binding');
  const verification = await verifyProductBrowser(join(candidateRoot, 'package'), { timedClock: manifest.app.id === 'ai.vibapp.custom.1a04442610c' });
  const derivation = manifest.artifacts.browser_derivations[0];
  const derivationDoc = await json(join(candidateRoot, 'package', derivation.derivation_attestation.path));
  const component = manifest.artifacts.canonical_component;
  const prefix = '/launcher/components/' + component.sha256 + '/';
  const fileMap = new Map();
  const files = [];
  for (const file of derivation.files) {
    const value = await readFile(join(candidateRoot, 'package', file.path));
    requireValue(value.length === file.size_bytes && hash(value) === file.sha256, 'file-integrity');
    const descriptor = { ...file, path: prefix + file.path.split('/').at(-1), format: file.path === derivation.entry.path ? 'jco-esm' : 'host-adapter-esm' };
    fileMap.set(descriptor.path, value); files.push(descriptor);
  }
  files.sort((a, b) => a.path.localeCompare(b.path));
  const launcher = manifest.entrypoints.find(item => item.kind === 'launcher-ui');
  const binding = { schema_version: 'vibapp.browser-derivation-binding.experimental-v1', source_kind: 'verifier-promoted-candidate',
    app_id: manifest.app.id, profile: 'web-runtime', launch_entrypoint: launcher.id, initial_route: launcher.routes.initial,
    preview_record_id: 'registry.public.' + manifest.app.id, preview_record_revision: 1,
    canonical_package_digest_sha256: digest, canonical_component: { media_type: component.media_type, sha256: component.sha256, size_bytes: component.size_bytes },
    derived_from_sha256: component.sha256, files, entry: files.find(item => item.format === 'jco-esm'), host_adapter: files.find(item => item.format === 'host-adapter-esm'),
    host_adapter_sha256: files.find(item => item.format === 'host-adapter-esm').sha256,
    attestation: { kind: 'product-verified-jco-derivation', trusted_builder_policy: 'vibapp.product-browser.stateless-v1', verification_state: 'verified',
      canonical_component_transformation_proven: true, product_activation_eligible: true, stage0_activation_eligible: false } };
  binding.attestation.binding_payload_sha256 = hash(canonicalJson(bindingPayload(binding)));
  const attestation = { schema_version: 'vibapp.product-browser-binding-attestation.v1', policy: 'vibapp.product-browser.stateless-v1',
    binding_payload_sha256: binding.attestation.binding_payload_sha256, canonical_package_digest_sha256: digest,
    independent_rederivation: true, manifest, derivation: derivationDoc, verification, stage0_activation_eligible: false };
  const attestationBytes = bytes(attestation);
  binding.attestation.artifact = { path: prefix + 'attestation-' + hash(attestationBytes) + '.json', media_type: 'application/json', sha256: hash(attestationBytes), size_bytes: attestationBytes.length };
  const evidence = value => ({ media_type: value.media_type, sha256: value.sha256, size_bytes: value.size_bytes, visibility: 'public' });
  const timestamp = candidate.verification.verified_at_utc;
  const record = { schema_version: 'vibapp.registry-record.product-v0.0.1', document_type: 'registry-record', record_id: binding.preview_record_id,
    record_revision: 1, created_at_utc: timestamp,
    app: { id: manifest.app.id, version: manifest.app.version, kind: manifest.app.kind, display_name: manifest.app.display_name, summary: manifest.app.description,
      publisher: { publisher_id: manifest.app.publisher.id, display_name: manifest.app.publisher.display_name, verification_state: 'verified' } },
    package: { app_id: manifest.app.id, version: manifest.app.version, package_digest_sha256: digest, manifest_digest_sha256: candidate.manifest.sha256 },
    compatibility: { profiles: manifest.runtime.profiles.map(row => row.profile), platforms: manifest.runtime.platforms.flatMap(row => row.profiles.map(profile => ({ os: row.os, arch: row.arch, profile }))) },
    contract: { package_format: manifest.package_format, component_contract: manifest.runtime.contract, wasi: manifest.runtime.wasi, wit_world: manifest.runtime.world },
    permissions: manifest.capabilities.map(row => ({ interface: row.interface, necessity: row.necessity, scope_digest_sha256: hash(canonicalJson(row.scope)), summary: row.reason })),
    publication: { state: 'published', public_metadata_digest_sha256: hash(canonicalJson({ app: manifest.app, package: digest, source })), published_at_utc: timestamp, revoked_at_utc: null },
    source: { visibility: 'public', license_spdx: manifest.license.spdx_expression, source_digest_sha256: candidate.source_tree_sha256, github_archive: source },
    verification: { status: 'verified', revocation: 'not-revoked', provenance: evidence(manifest.artifacts.provenance), sbom: evidence(manifest.artifacts.sbom),
      scan: evidence({ media_type: 'application/json', sha256: hash(candidateBytes), size_bytes: candidateBytes.length }),
      evidence: [{ evidence_id: 'browser.rederivation.' + digest, kind: 'independent-browser-verifier', sha256: hash(bytes(verification)) }] },
    search_metadata: { tags: ['web-runtime', 'offline'], capability_labels: manifest.capabilities.map(row => row.interface), search_text_digest_sha256: hash(manifest.app.description) } };
  requireValue(isPublicRegistryRecord(record) && isRealPublicBrowserBinding(record, binding), 'registry-trust');
  await verifyBrowserPreviewBinding(record, binding);
  await verifyDerivationFileSet(binding, fileMap);
  await verifyDerivationAttestation(binding, attestationBytes);
  fileMap.set(binding.attestation.artifact.path, attestationBytes);
  for (const [path, value] of fileMap) {
    const target = join(outputRoot, 'public', path.slice(1));
    await mkdir(join(target, '..'), { recursive: true }); await writeFile(target, value);
    const retained = join(outputRoot, 'published-components', path.slice('/launcher/components/'.length));
    await mkdir(join(retained, '..'), { recursive: true }); await writeFile(retained, value);
  }
  const inventory = receipt.verified_downloads.filter(row => row.path && !row.path.startsWith('metadata/')).map(row => ({ path: row.path, sha256: row.sha256, size_bytes: row.size_bytes }));
  const locator = { schema_version: 'vibapp.roomhash-package-locator.experimental-v1', package_digest_sha256: digest, transport: 'bittorrent-v1',
    info_hash: receipt.info_hash, magnet_uri: receipt.magnet_uri, size_bytes: inventory.reduce((total, row) => total + row.size_bytes, 0), files: inventory,
    trust_note: 'locator-only-package-bytes-require-vibapp-verification' };
  const snapshot = { snapshot_version: 'vibapp.registry.snapshot.product-v1', corpus_version: 'public-package-releases', generated_at_utc: timestamp,
    platform_status: 'product-browser-qualified', records: [record], browser_artifact_bindings: [binding], browser_preview_records: [], sample_search: {},
    consumer_notes: { website_projection_mode: 'production-public-only', local_private_preview_enabled: false } };
  await mkdir(join(outputRoot, 'package-locators'), { recursive: true });
  await writeFile(join(outputRoot, 'package-locators', digest + '.json'), bytes(locator));
  await writeFile(join(outputRoot, 'registry.snapshot.json'), bytes(snapshot));
  await syncPublicRegistryDataRoot({ registryBytes: bytes(snapshot), sourceDirectory: join(outputRoot, 'package-locators'), targetRoot: join(outputRoot, 'public/data'), productionOnly: true });
  return { app_id: binding.app_id, package_digest_sha256: digest, output_root: outputRoot, files: [...fileMap.keys()], verification };
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  process.stdout.write(canonicalJson(await projectBrowserRelease(resolve(process.argv[2]), resolve(process.argv[3]), resolve(process.argv[4]))) + '\n');
}
