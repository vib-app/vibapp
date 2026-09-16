// Host-owned, content-addressed public runtime cache. Never cache API calls,
// credentials, mutable application state, HTML, or arbitrary package paths.
const CACHE = 'vibapp-verified-runtime-v1';
const pathPattern = /^\/launcher\/components\/[0-9a-f]{64}\/(?:app|host-adapter|derivation|attestation)-([0-9a-f]{64})\.(?:jco\.mjs|mjs|json)$/;
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  const match = url.origin === self.location.origin && event.request.method === 'GET' && !url.search && pathPattern.exec(url.pathname);
  if (!match) return;
  event.respondWith((async () => {
    const cache = await caches.open(CACHE);
    const cached = await cache.match(url.href);
    for (const response of [cached, null]) {
      const value = response || await fetch(event.request, { credentials: 'omit', redirect: 'error' });
      if (!value.ok) return value;
      const reader = value.body.getReader();
      const chunks = []; let size = 0;
      try {
        for (;;) {
          const { done, value: chunk } = await reader.read(); if (done) break;
          size += chunk.length; if (size > 64 * 1024 * 1024) throw new Error('Runtime file too large');
          chunks.push(chunk);
        }
      } finally { await reader.cancel().catch(() => {}); }
      const bytes = new Uint8Array(size); let offset = 0;
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
      const digest = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), n => n.toString(16).padStart(2, '0')).join('');
      if (digest !== match[1]) {
        await cache.delete(url.href);
        if (response) continue;
        return new Response('Runtime integrity check failed', { status: 502 });
      }
      const verified = new Response(bytes, { headers: { 'Content-Type': url.pathname.endsWith('.json') ? 'application/json' : 'text/javascript', 'Cache-Control': 'public, max-age=31536000, immutable', 'X-VibApp-Size': String(size) } });
      await cache.put(url.href, verified.clone());
      let total = 0;
      for (const key of (await cache.keys()).reverse()) {
        total += Number((await cache.match(key))?.headers.get('X-VibApp-Size') || 64 * 1024 * 1024);
        if (total > 256 * 1024 * 1024) await cache.delete(key);
      }
      return verified;
    }
  })());
});
