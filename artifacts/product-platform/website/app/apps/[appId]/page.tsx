import type { Metadata } from 'next';
import { notFound } from 'next/navigation';
import registrySnapshot from '../../../public/data/registry.snapshot.json';
import PreviewFrame from '../../preview-frame';
import { getPreviewOrigin } from '../../../lib/preview-origin';
import { readStoreCatalog } from '../../../lib/store-catalog';

type ShareRecord = {
  document_type: string;
  schema_version: string;
  app: { id: string; display_name: string; summary: string };
  package: { app_id: string; package_digest_sha256: string };
  compatibility: { profiles: string[] };
  publication: { state: string };
  verification: { status: string; revocation?: string };
  source?: { visibility?: string; source_digest_sha256?: string; github_archive?: {
    organization?: string; repository?: string; repository_id?: number; commit_sha?: string;
    source_digest_sha256?: string; package_digest_sha256?: string;
  } };
};

type BrowserBinding = {
  schema_version?: string;
  source_kind?: string;
  app_id?: string;
  profile?: string;
  canonical_package_digest_sha256?: string;
  canonical_component?: { sha256?: string };
  derived_from_sha256?: string;
  entry?: { path?: string; sha256?: string; size_bytes?: number; format?: string };
  files?: Array<{ path?: string; sha256?: string; size_bytes?: number; format?: string }>;
  attestation?: {
    kind?: string;
    trusted_builder_policy?: string;
    product_activation_eligible?: boolean;
    verification_state?: string;
    canonical_component_transformation_proven?: boolean;
    stage0_activation_eligible?: boolean;
    binding_payload_sha256?: string;
    artifact?: { path?: string; sha256?: string; size_bytes?: number };
  };
};

const SHA256 = /^[0-9a-f]{64}$/;
const records = registrySnapshot.records as unknown as ShareRecord[];
const previewRecords = registrySnapshot.browser_preview_records as unknown as ShareRecord[];
const bindings = registrySnapshot.browser_artifact_bindings as unknown as BrowserBinding[];
const consumerNotes = registrySnapshot.consumer_notes as unknown as {
  website_projection_mode?: string;
  website_public_shareable_app_ids?: string[];
  local_private_preview_enabled?: boolean;
};

function descriptorIsBound(value: BrowserBinding['entry'], prefix: string) {
  return typeof value?.path === 'string'
    && value.path.startsWith(prefix)
    && SHA256.test(value.sha256 || '')
    && Number.isInteger(value.size_bytes)
    && Number(value.size_bytes) > 0
    && Number(value.size_bytes) <= 4 * 1024 * 1024;
}

function isPublicRecord(record: ShareRecord) {
  return record.document_type === 'registry-record'
    && record.publication?.state === 'published'
    && record.verification?.status === 'verified'
    && record.verification?.revocation === 'not-revoked'
    && ['private', 'public'].includes(record.source?.visibility || '')
    && record.source?.github_archive?.organization === 'vib-app'
    && ((record.source.github_archive.repository === 'sources' && record.source.visibility === 'public')
      || /^app-[0-9a-f]{64}$/.test(record.source.github_archive.repository || ''))
    && Number.isSafeInteger(record.source.github_archive.repository_id)
    && Number(record.source.github_archive.repository_id) > 0
    && /^[0-9a-f]{40}$/.test(record.source.github_archive.commit_sha || '')
    && SHA256.test(record.source.github_archive.source_digest_sha256 || '')
    && record.source.github_archive.source_digest_sha256 === record.source.source_digest_sha256
    && record.source.github_archive.package_digest_sha256 === record.package?.package_digest_sha256
    && record.package?.app_id === record.app?.id
    && SHA256.test(record.package?.package_digest_sha256 || '');
}

function isRealPublicBrowserBinding(record: ShareRecord, binding: BrowserBinding) {
  const prefix = '/launcher/components/' + String(binding.canonical_component?.sha256 || '') + '/';
  return binding.schema_version === 'vibapp.browser-derivation-binding.experimental-v1'
    && binding.source_kind === 'verifier-promoted-candidate'
    && binding.app_id === record.app.id
    && binding.canonical_package_digest_sha256 === record.package.package_digest_sha256
    && (binding.profile === 'web-preview' || binding.profile === 'web-runtime')
    && record.compatibility.profiles.includes(binding.profile)
    && SHA256.test(binding.canonical_component?.sha256 || '')
    && binding.derived_from_sha256 === binding.canonical_component?.sha256
    && descriptorIsBound(binding.entry, prefix)
    && Array.isArray(binding.files)
    && binding.files.length >= 1
    && binding.files.length <= 32
    && binding.files.every(file => descriptorIsBound(file, prefix))
    && binding.files.some(file => file.path === binding.entry?.path && file.sha256 === binding.entry?.sha256)
    && binding.attestation?.verification_state === 'verified'
    && binding.attestation?.canonical_component_transformation_proven === true
    && (binding.attestation?.stage0_activation_eligible === true
      || (binding.profile === 'web-runtime'
        && binding.attestation?.kind === 'product-verified-jco-derivation'
        && binding.attestation?.trusted_builder_policy === 'vibapp.product-browser.stateless-v1'
        && binding.attestation?.product_activation_eligible === true
        && binding.attestation?.stage0_activation_eligible === false))
    && SHA256.test(binding.attestation?.binding_payload_sha256 || '')
    && descriptorIsBound(binding.attestation.artifact, prefix);
}

function explicitLoopbackLocalBuild() {
  if (process.env.VIBAPP_PREVIEW_LOCAL !== '1') return false;
  try {
    const origin = new URL(process.env.VIBAPP_PREVIEW_ORIGIN || '');
    return origin.protocol === 'http:'
      && origin.hostname === '127.0.0.1'
      && origin.port !== ''
      && origin.pathname === '/'
      && !origin.username
      && !origin.password
      && !origin.search
      && !origin.hash;
  } catch {
    return false;
  }
}

function isLoopbackPrivateRecord(record: ShareRecord) {
  return explicitLoopbackLocalBuild()
    && consumerNotes.website_projection_mode === 'loopback-local-development'
    && consumerNotes.local_private_preview_enabled === true
    && record.schema_version === 'vibapp.browser-preview-record.experimental-v1'
    && record.document_type === 'browser-preview-record-projection'
    && record.publication?.state === 'private-candidate'
    && record.verification?.status === 'locally-derived-awaiting-independent-verifier';
}

function isLoopbackPrivateBinding(record: ShareRecord, binding: BrowserBinding) {
  return binding.schema_version === 'vibapp.browser-derivation-binding.experimental-v1'
    && binding.source_kind === 'verifier-promoted-candidate'
    && binding.app_id === record.app.id
    && binding.profile === 'web-preview'
    && binding.canonical_package_digest_sha256 === record.package.package_digest_sha256
    && binding.entry?.format === 'jco-esm'
    && Array.isArray(binding.files)
    && binding.files.some(file => file.path === binding.entry?.path && file.sha256 === binding.entry?.sha256)
    && binding.attestation?.verification_state === 'locally-derived-awaiting-independent-verifier'
    && binding.attestation?.canonical_component_transformation_proven === true
    && binding.attestation?.stage0_activation_eligible === false;
}

const declaredPublicIds = new Set(consumerNotes.website_public_shareable_app_ids || []);
const publicShareableRecords = records.filter(record => (
  isPublicRecord(record)
  && declaredPublicIds.has(record.app.id)
  && bindings.some(binding => isRealPublicBrowserBinding(record, binding))
));
const localPrivateRecords = previewRecords.filter(record => (
  isLoopbackPrivateRecord(record)
  && bindings.some(binding => isLoopbackPrivateBinding(record, binding))
));
const shareableRecords = [...publicShareableRecords, ...localPrivateRecords];

export function generateStaticParams() {
  return shareableRecords.map(record => ({ appId: record.app.id }));
}

export async function generateMetadata({ params }: { params: Promise<{ appId: string }> }): Promise<Metadata> {
  const { appId } = await params;
  const listed = (await readStoreCatalog()).apps.find(app => app.app_id === appId);
  if (listed) return { title: listed.display_name + ' · VibApp', description: listed.summary };
  const record = shareableRecords.find(item => item.app.id === appId);
  return record ? {
    title: record.app.display_name + ' · VibApp',
    description: record.app.summary,
    openGraph: {
      title: record.app.display_name + ' · VibApp',
      description: record.app.summary,
      images: ['/og.png'],
    },
  } : { title: 'VibApp' };
}

export default async function SharedVibApp({ params }: { params: Promise<{ appId: string }> }) {
  const { appId } = await params;
  const record = shareableRecords.find(item => item.app.id === appId);
  const listed = !record && (await readStoreCatalog()).apps.some(app => app.app_id === appId);
  if (!record && !listed) notFound();
  return (
    <main className="web-client-host">
      <PreviewFrame appId={appId} previewOrigin={getPreviewOrigin()} title={'VibApp · ' + appId} trustedShellOnly={process.env.NEXT_PUBLIC_VIBAPP_HOSTED_SHELL === '1'} />
    </main>
  );
}
