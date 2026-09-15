const REASONS = new Set([
  'extra-authority',
  'replay',
  'tamper',
  'wrong-nonce',
  'wrong-origin',
]);

const SOURCES = new Set([
  'app-worker',
  'launcher-runtime',
  'preview-frame',
]);

const pendingImages = new Set();
let remainingReports = 16;

export function classifyInvalidBootstrapError(error) {
  const message = error instanceof Error ? error.message : String(error || '');
  if (/keys|extra-authority/.test(message)) return 'extra-authority';
  if (/nonce/.test(message)) return 'wrong-nonce';
  if (/origin/.test(message)) return 'wrong-origin';
  if (/sequence|replay|request-id-mismatch/.test(message)) return 'replay';
  if (/integrity-failure|malformed-output|preview-bind-rejected|browser-derivation-failure/.test(message)) return 'tamper';
  return null;
}

export function reportInvalidBootstrap(reason, source) {
  if (!REASONS.has(reason) || !SOURCES.has(source) || remainingReports <= 0) return false;
  remainingReports -= 1;
  const url = new URL('/audit/invalid-bootstrap.gif', globalThis.location.origin);
  url.searchParams.set('schema_version', 'vibapp.invalid-bootstrap-report.experimental-v1');
  url.searchParams.set('source', source);
  url.searchParams.set('reason', reason);
  if (typeof Image === 'function') {
    const beacon = new Image(1, 1);
    pendingImages.add(beacon);
    const release = () => pendingImages.delete(beacon);
    beacon.onload = release;
    beacon.onerror = release;
    beacon.referrerPolicy = 'no-referrer';
    beacon.src = url.href;
    return true;
  }
  if (typeof fetch === 'function') {
    void fetch(url, {
      method: 'GET',
      cache: 'no-store',
      credentials: 'omit',
      redirect: 'error',
      referrerPolicy: 'no-referrer',
    }).catch(() => {});
    return true;
  }
  return false;
}
