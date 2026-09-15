// Discovery metadata only: never interpret a catalog row as runtime authority.
import snapshot from './store-catalog.snapshot.json' with { type: 'json' };
export const CATALOG_URL = 'https://raw.githubusercontent.com/vib-app/packages/main/registry.json';
const MAX_BYTES = 1024 * 1024;
const SHA256 = /^[0-9a-f]{64}$/;
const SHA1 = /^[0-9a-f]{40}$/;

export type StoreApp = {
  app_id: string; display_name: string; summary: string; version: string;
  kind: string; publisher: string; package_digest_sha256: string; component_sha256: string;
  source_url: string; release_url: string; download_url: string; download_sha256: string;
  publication_state: string; publication_badge: string; verification_state: string;
  verification_summary: string; permissions: string[]; launch_eligible: false; install_eligible: false;
  browser_runtime_available: boolean;
};

function text(value: unknown, max: number): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= max && !/[\u0000-\u001f]/.test(value);
}

export function parseCatalog(value: unknown): StoreApp[] {
  if (!value || typeof value !== 'object') throw new Error('store-catalog-invalid');
  const catalog = value as { schema_version?: string; apps?: unknown[] };
  if (catalog.schema_version !== 'vibapp.store-catalog.v1' || !Array.isArray(catalog.apps) || catalog.apps.length > 500) {
    throw new Error('store-catalog-invalid');
  }
  const identities = new Set();
  return catalog.apps.flatMap(item => {
    if (!item || typeof item !== 'object') throw new Error('store-entry-invalid');
    const app = item as Record<string, any>;
    if (!text(app.app_id, 128) || !/^[a-z0-9][a-z0-9._-]+$/.test(app.app_id) || identities.has(app.app_id)) {
      throw new Error('store-identity-invalid');
    }
    identities.add(app.app_id);
    // Removing a row or marking it revoked removes it from discovery immediately.
    if (app.publication_state !== 'published') return [];
    const digest = app.package_digest_sha256;
    const source = app.source;
    const release = `https://github.com/vib-app/packages/releases/tag/vibapp-package-${digest}`;
    const download = `https://github.com/vib-app/packages/releases/download/vibapp-package-${digest}/${digest}.zip`;
    if (!text(app.display_name, 200) || !text(app.summary, 4000) || !text(app.version, 100)
      || !text(app.publisher, 128) || !['ui', 'service', 'hybrid'].includes(app.kind)
      || !SHA256.test(digest || '') || !SHA256.test(app.component_sha256 || '')
      || app.verification_state !== 'package-verified' || typeof app.browser_runtime_available !== 'boolean'
      || source?.organization !== 'vib-app' || source.repository !== 'sources'
      || source.repository_id !== 1371257005 || !SHA1.test(source.commit_sha || '')
      || !SHA256.test(source.source_digest_sha256 || '') || source.package_digest_sha256 !== digest
      || app.source_url !== `https://github.com/vib-app/sources/tree/${source.commit_sha}`
      || app.release_url !== release || app.download?.url !== download
      || !SHA256.test(app.download.sha256 || '') || !Number.isSafeInteger(app.download.size_bytes)
      || app.download.size_bytes <= 0 || app.download.size_bytes > 64 * 1024 * 1024) {
      throw new Error('store-entry-invalid');
    }
    if (!Array.isArray(app.permissions) || app.permissions.length > 64
      || !app.permissions.every((item: unknown) => text(item, 200) && /^vibapp:[a-z0-9@./-]+$/.test(item))) {
      throw new Error('store-permissions-invalid');
    }
    return [{
      app_id: app.app_id, display_name: app.display_name, summary: app.summary, version: app.version,
      kind: app.kind, publisher: app.publisher, package_digest_sha256: digest, component_sha256: app.component_sha256,
      source_url: app.source_url, release_url: release, download_url: download, download_sha256: app.download.sha256,
      publication_state: 'published', publication_badge: 'public-appstore', verification_state: 'package-verified',
      verification_summary: 'Package structure, Component imports and public download bytes checked. Browser activation requires a separate verified runtime binding. / 已校验包结构、组件接口和公开下载内容；网页运行另需已验证的运行包。',
      permissions: app.permissions.map((item: string) => item.replace('vibapp:experimental-v0/', '').replace('@0.0.1', '')),
      launch_eligible: false as const, install_eligible: false as const, browser_runtime_available: app.browser_runtime_available,
    }];
  });
}

export async function readStoreCatalog(): Promise<{ apps: StoreApp[]; feed_errors: string[] }> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);
  let transportComplete = false;
  try {
    // Fixed public repository, no bearer token, cookies, redirect or input URL.
    const response = await fetch(CATALOG_URL, {
      // workerd supports follow/manual, not redirect:error. Manual plus the
      // non-2xx check below rejects redirects without issuing another request.
      signal: controller.signal, redirect: 'manual',
      headers: { Accept: 'application/json', 'Cache-Control': 'no-cache', 'User-Agent': 'VibApp-Store-Catalog' },
    });
    transportComplete = true;
    if (!response.ok || !response.body || Number(response.headers.get('content-length') || 0) > MAX_BYTES) {
      throw new Error('store-feed-unavailable');
    }
    const reader = response.body.getReader();
    const chunks: Uint8Array[] = [];
    let length = 0;
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        length += value.byteLength;
        if (length > MAX_BYTES) throw new Error('store-feed-limit');
        chunks.push(value);
      }
    } catch (error) { await reader.cancel(); throw error; }
    finally { reader.releaseLock(); }
    const bytes = new Uint8Array(length);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    transportComplete = true;
    return { apps: parseCatalog(JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes))), feed_errors: [] };
  } catch (error) {
    // Public discovery snapshot only, copied from catalog commit d65a6c158ddb450d2483f39e39dcdac9d7941ee5.
    // Never fall back after a malformed/revoked live catalog, nor grant runtime authority.
    console.warn('Public Store catalog read failed:', error instanceof Error ? error.message.slice(0, 180) : 'unknown');
    return transportComplete
      ? { apps: [], feed_errors: ['store-catalog-unavailable'] }
      : { apps: parseCatalog(snapshot), feed_errors: ['store-catalog-snapshot'] };
  } finally { clearTimeout(timer); }
}
