import test from 'node:test';
import assert from 'node:assert/strict';
import { parseCatalog, readStoreCatalog, CATALOG_URL } from '../lib/store-catalog.ts';

// Synthetic metadata tests only; never shipped as a Store entry.
function catalog() {
  const digest = 'a'.repeat(64);
  const commit = 'b'.repeat(40);
  return { schema_version: 'vibapp.store-catalog.v1', apps: [{
    app_id: 'ai.vibapp.test', display_name: 'Test', summary: 'Test package', version: '1.0.0',
    kind: 'ui', publisher: 'Test publisher', package_digest_sha256: digest, component_sha256: 'c'.repeat(64),
    source: { organization: 'vib-app', repository: 'sources', repository_id: 1371257005, commit_sha: commit,
      source_digest_sha256: 'd'.repeat(64), package_digest_sha256: digest },
    source_url: `https://github.com/vib-app/sources/tree/${commit}`,
    release_url: `https://github.com/vib-app/packages/releases/tag/vibapp-package-${digest}`,
    download: { url: `https://github.com/vib-app/packages/releases/download/vibapp-package-${digest}/${digest}.zip`,
      sha256: 'e'.repeat(64), size_bytes: 1234 },
    publication_state: 'published', verification_state: 'package-verified', browser_runtime_available: false,
    permissions: ['vibapp:experimental-v0/clock@0.0.1'],
  }] };
}

test('listed package is discoverable but never grants install or browser authority', () => {
  const [app] = parseCatalog(catalog());
  assert.equal(app.publication_state, 'published');
  assert.equal(app.launch_eligible, false);
  assert.equal(app.install_eligible, false);
  assert.equal(app.browser_runtime_available, false);
  assert.deepEqual(app.permissions, ['clock']);
});

test('browser metadata alone never enables execution; invalid metadata fails closed', () => {
  const browser = catalog(); browser.apps[0].browser_runtime_available = true;
  assert.equal(parseCatalog(browser)[0].launch_eligible, false);
  const revoked = catalog(); revoked.apps[0].publication_state = 'revoked';
  assert.deepEqual(parseCatalog(revoked), []);
  const duplicate = catalog(); duplicate.apps.push(duplicate.apps[0]);
  assert.throws(() => parseCatalog(duplicate));
  for (const mutate of [
    app => { app.source.repository = 'packages'; },
    app => { app.download.url = 'https://evil.example/app.zip'; },
    app => { app.source.package_digest_sha256 = 'f'.repeat(64); },
    app => { app.browser_runtime_available = 'true'; },
    app => { app.permissions = ['<script>']; },
  ]) {
    const value = catalog(); mutate(value.apps[0]); assert.throws(() => parseCatalog(value));
  }
});

test('fixed public feed is bounded and never sends authentication', async () => {
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async (url, options) => {
      assert.equal(url, CATALOG_URL);
      assert.equal(options.credentials, undefined); assert.equal(options.redirect, 'manual');
      assert.equal(options.headers.Authorization, undefined);
      assert.equal(options.headers.Cookie, undefined);
      return Response.json(catalog());
    };
    assert.equal((await readStoreCatalog()).apps.length, 1);
    globalThis.fetch = async () => new Response(null, { status: 302, headers: { Location: 'https://evil.example' } });
    assert.deepEqual(await readStoreCatalog(), { apps: [], feed_errors: ['store-catalog-unavailable'] });
    globalThis.fetch = async () => { throw new TypeError('fetch failed'); };
    const cached = await readStoreCatalog();
    assert.deepEqual(cached.feed_errors, ['store-catalog-snapshot']);
    assert.equal(cached.apps[0].launch_eligible, false);
    globalThis.fetch = async () => new Response('x'.repeat(1024 * 1024 + 1));
    assert.deepEqual(await readStoreCatalog(), { apps: [], feed_errors: ['store-catalog-unavailable'] });
  } finally { globalThis.fetch = original; }
});
