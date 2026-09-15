import { createServer } from 'node:http';
import { createHash } from 'node:crypto';
import { lstat, readFile, realpath } from 'node:fs/promises';
import { isAbsolute, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  DurableInvalidBootstrapAudit,
  validateAuditReport,
} from './invalid-bootstrap-audit.mjs';
import { previewBoundary, validateExactOrigin, validateGuiSyncManifest } from './preview-protocol.mjs';

const HERE = fileURLToPath(new URL('./', import.meta.url));
const DEFAULT_PUBLIC_ROOT = resolve(HERE, '../product-platform/website/public');
const PRODUCTION_PREVIEW_ORIGIN = 'https://preview.vibapp.ai';
const PRODUCTION_PARENT_ORIGIN = 'https://vibapp.ai';
const LOCAL_HOSTS = new Set(['127.0.0.1', 'localhost', '[::1]']);
const OWN_STATIC = new Map([
  ['/preview-broker.mjs', resolve(HERE, 'preview-broker.mjs')],
  ['/preview-protocol.mjs', resolve(HERE, 'preview-protocol.mjs')],
  ['/preview-shell.css', resolve(HERE, 'preview-shell.css')],
]);
const LAUNCHER_FILES = new Set([
  'app-runtime-worker.js',
  'foreground-session.mjs',
  'app.js',
  'favicon.svg',
  'gui-sync-manifest.json',
  'index.html',
  'invalid-bootstrap-audit.mjs',
  'runtime-integrity.mjs',
  'styles.css',
  'vibapp_web_client_core.wasm',
  'web-bridge.js',
]);
const MIME = new Map([
  ['.css', 'text/css; charset=utf-8'],
  ['.html', 'text/html; charset=utf-8'],
  ['.js', 'text/javascript; charset=utf-8'],
  ['.json', 'application/json; charset=utf-8'],
  ['.mjs', 'text/javascript; charset=utf-8'],
  ['.svg', 'image/svg+xml'],
  ['.wasm', 'application/wasm'],
]);
const DEFAULT_AUDIT_PATH = resolve(HERE, 'output', 'invalid-bootstrap-audit.jsonl');

function configurationError(detail) {
  throw new Error('preview-host-configuration:' + detail);
}

function requireEnvironment(env, name) {
  const value = env[name];
  if (typeof value !== 'string' || value.trim() === '') configurationError(name + '-required');
  return value.trim();
}

function parseConfiguredOrigin(value, name) {
  try {
    return new URL(validateExactOrigin(value, name));
  } catch {
    configurationError(name + '-must-be-an-exact-origin');
  }
}

function requireLocalOrigin(url, name) {
  if (url.protocol !== 'http:' || !LOCAL_HOSTS.has(url.hostname)) {
    configurationError(name + '-must-be-explicit-loopback-http-outside-production');
  }
  if (!url.port) configurationError(name + '-must-include-local-port');
}

export function loadConfig(env = process.env) {
  const production = env.NODE_ENV === 'production';
  const previewUrl = parseConfiguredOrigin(requireEnvironment(env, 'VIBAPP_PREVIEW_ORIGIN'), 'preview-origin');
  const parentUrl = parseConfiguredOrigin(requireEnvironment(env, 'VIBAPP_PARENT_ORIGIN'), 'parent-origin');

  if (production) {
    if (previewUrl.origin !== PRODUCTION_PREVIEW_ORIGIN) configurationError('production-preview-origin-must-be-' + PRODUCTION_PREVIEW_ORIGIN);
    if (parentUrl.origin !== PRODUCTION_PARENT_ORIGIN) configurationError('production-parent-origin-must-be-' + PRODUCTION_PARENT_ORIGIN);
  } else {
    requireLocalOrigin(previewUrl, 'preview-origin');
    requireLocalOrigin(parentUrl, 'parent-origin');
  }

  const portText = env.VIBAPP_PREVIEW_PORT || (production ? '8788' : previewUrl.port);
  const port = Number(portText);
  if (!Number.isInteger(port) || port < 1 || port > 65535) configurationError('preview-port-invalid');
  if (!production && String(port) !== previewUrl.port) configurationError('preview-port-must-match-local-origin');
  const listenHost = env.VIBAPP_PREVIEW_LISTEN_HOST || '127.0.0.1';
  if (!production && !LOCAL_HOSTS.has(listenHost)) configurationError('local-listen-host-must-be-loopback');
  const publicRoot = resolve(env.VIBAPP_WEB_PUBLIC_ROOT || DEFAULT_PUBLIC_ROOT);
  if (!isAbsolute(publicRoot)) configurationError('public-root-must-be-absolute');
  const configuredAuditPath = env.VIBAPP_PREVIEW_AUDIT_PATH;
  if (production && (typeof configuredAuditPath !== 'string' || configuredAuditPath.trim() === '')) {
    configurationError('production-audit-path-required');
  }
  if (configuredAuditPath !== undefined && (
    typeof configuredAuditPath !== 'string'
    || configuredAuditPath.trim() === ''
    || !isAbsolute(configuredAuditPath.trim())
  )) {
    configurationError('audit-path-must-be-absolute');
  }
  const auditPath = configuredAuditPath ? configuredAuditPath.trim() : DEFAULT_AUDIT_PATH;

  return Object.freeze({
    production,
    previewOrigin: previewUrl.origin,
    previewHost: previewUrl.host,
    parentOrigin: parentUrl.origin,
    listenHost,
    port,
    publicRoot,
    auditPath,
  });
}

function htmlEscape(value) {
  return value.replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
}

function previewHtml(config, guiCacheVersion) {
  return `<!doctype html>
<html lang="zh-CN" data-parent-origin="${htmlEscape(config.parentOrigin)}" data-gui-cache-version="${htmlEscape(guiCacheVersion)}">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="color-scheme" content="light dark">
    <title>VibApp isolated preview</title>
    <link rel="stylesheet" href="/preview-shell.css">
    <script type="module" src="/preview-broker.mjs"></script>
  </head>
  <body>
    <main class="preview-shell">
      <div class="preview-boundary" role="status" aria-live="polite">
        <strong>Preview · no local/background access</strong>
        <span data-preview-status>等待受信任的官网来源绑定一次性预览会话…</span>
      </div>
      <iframe class="preview-frame" data-preview-launcher title="VibApp Web GUI" credentialless sandbox="allow-scripts allow-same-origin allow-forms" referrerpolicy="no-referrer"></iframe>
    </main>
  </body>
</html>
`;
}

function cspFor(pathname, config) {
  if (pathname === '/' || pathname === '/preview.html') {
    return "default-src 'none'; script-src 'self'; frame-src 'self'; img-src 'self'; style-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors " + config.parentOrigin;
  }
  if (pathname === '/launcher/index.html') {
    // Jco still needs Wasm compilation and the launcher rehashes same-origin static
    // artifacts. No executable Blob/data module URL is allowed.
    return "default-src 'none'; img-src 'self' data:; style-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self' " + config.parentOrigin + "; manifest-src 'none'";
  }
  if (pathname === '/launcher/app-runtime-worker.js') {
    return "default-src 'none'; script-src 'self' 'wasm-unsafe-eval'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";
  }
  return "default-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";
}

function responseHeaders(pathname, config, contentType, length, cacheControl = 'no-store') {
  return {
    'Cache-Control': cacheControl,
    'Content-Length': String(length),
    'Content-Security-Policy': cspFor(pathname, config),
    'Content-Type': contentType,
    'Cross-Origin-Resource-Policy': 'same-origin',
    'Origin-Agent-Cluster': '?1',
    'Permissions-Policy': 'camera=(), microphone=(), geolocation=(), payment=(), usb=(), serial=(), bluetooth=(), midi=(), clipboard-read=(), clipboard-write=()',
    'Referrer-Policy': 'no-referrer',
    'X-Content-Type-Options': 'nosniff',
    'X-VibApp-Preview-Isolation': 'partial',
  };
}

function sendBytes(request, response, status, pathname, config, bytes, contentType, cacheControl) {
  response.writeHead(status, responseHeaders(pathname, config, contentType, bytes.byteLength, cacheControl));
  if (request.method === 'HEAD') response.end();
  else response.end(bytes);
}

function sendJson(request, response, status, pathname, config, value) {
  const bytes = Buffer.from(JSON.stringify(value) + '\n');
  sendBytes(request, response, status, pathname, config, bytes, 'application/json; charset=utf-8', 'no-store');
}

function safeRequestPath(request) {
  const rawPath = String(request.url || '/').split('?', 1)[0];
  let decoded;
  try {
    decoded = decodeURIComponent(rawPath);
  } catch {
    return null;
  }
  if (!decoded.startsWith('/') || decoded.includes('\\') || decoded.includes('\0')) return null;
  if (decoded.split('/').some(segment => segment === '.' || segment === '..')) return null;
  return decoded;
}

function allowedPublicPath(pathname) {
  if (pathname === '/data/registry.snapshot.json') return true;
  if (pathname === '/data/package-locators/index.json') return true;
  const simple = pathname.match(/^\/launcher\/([^/]+)$/);
  if (simple) return LAUNCHER_FILES.has(simple[1]);
  return /^\/launcher\/components\/[0-9a-f]{64}\/[a-z0-9][a-z0-9.-]{0,191}\.(?:json|mjs)$/.test(pathname);
}

function extension(pathname) {
  const index = pathname.lastIndexOf('.');
  return index === -1 ? '' : pathname.slice(index);
}

async function readRegularFile(path, allowedRoot) {
  const requestedStat = await lstat(path);
  if (!requestedStat.isFile() || requestedStat.isSymbolicLink()) throw new Error('not-regular');
  const root = await realpath(allowedRoot);
  const target = await realpath(path);
  if (target !== root && !target.startsWith(root + sep)) throw new Error('outside-root');
  return readFile(target);
}

function verifyContentAddressedComponent(pathname, bytes) {
  if (!pathname.includes('/launcher/components/')) return;
  const name = pathname.split('/').at(-1);
  const match = name.match(/-([0-9a-f]{64})(?:\.[a-z0-9]+)+$/);
  if (!match) throw new Error('component-path-not-content-addressed');
  const actual = createHash('sha256').update(bytes).digest('hex');
  if (actual !== match[1]) throw new Error('component-path-digest-mismatch');
}

async function serve(request, response, config, audit) {
  if (request.headers.cookie !== undefined || request.headers.authorization !== undefined || request.headers['proxy-authorization'] !== undefined) {
    sendJson(request, response, 400, '/error', config, { error: 'credentials-forbidden' });
    return;
  }
  if (request.headers['service-worker'] !== undefined || request.headers['sec-fetch-dest'] === 'serviceworker') {
    sendJson(request, response, 403, '/error', config, { error: 'service-worker-forbidden' });
    return;
  }
  if (request.headers.host?.toLowerCase() !== config.previewHost.toLowerCase()) {
    sendJson(request, response, 421, '/error', config, { error: 'misdirected-preview-host' });
    return;
  }
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    sendJson(request, response, 405, '/error', config, { error: 'method-not-allowed' });
    return;
  }
  const pathname = safeRequestPath(request);
  if (!pathname) {
    sendJson(request, response, 400, '/error', config, { error: 'invalid-path' });
    return;
  }

  if (!audit.store.healthy) {
    sendJson(request, response, 503, '/error', config, {
      error: 'invalid-bootstrap-audit-unavailable',
      preview_activation: 'fail-closed',
    });
    return;
  }

  if (pathname === '/audit/invalid-bootstrap.gif') {
    if (request.method !== 'GET') {
      sendJson(request, response, 405, '/error', config, { error: 'method-not-allowed' });
      return;
    }
    let report;
    try {
      const requestUrl = new URL(request.url, config.previewOrigin);
      report = validateAuditReport(requestUrl.searchParams);
      await audit.store.record(report);
    } catch {
      sendJson(request, response, audit.store.healthy ? 400 : 503, '/error', config, {
        error: audit.store.healthy ? 'invalid-audit-report' : 'invalid-bootstrap-audit-unavailable',
      });
      return;
    }
    sendBytes(request, response, 204, pathname, config, Buffer.alloc(0), 'image/gif', 'no-store');
    return;
  }

  if (pathname === '/' || pathname === '/preview.html') {
    try {
      const manifestPath = resolve(config.publicRoot, 'launcher', 'gui-sync-manifest.json');
      const manifestBytes = await readRegularFile(manifestPath, config.publicRoot);
      const guiCacheVersion = validateGuiSyncManifest(JSON.parse(manifestBytes.toString('utf8')));
      const bytes = Buffer.from(previewHtml(config, guiCacheVersion));
      sendBytes(request, response, 200, pathname, config, bytes, 'text/html; charset=utf-8', 'no-store');
    } catch {
      sendJson(request, response, 503, '/error', config, { error: 'gui-sync-manifest-invalid' });
    }
    return;
  }
  if (pathname === '/healthz') {
    sendJson(request, response, 200, pathname, config, {
      status: 'ok',
      ...previewBoundary(),
      invalid_bootstrap_audit: audit.store.status(),
    });
    return;
  }
  if (pathname === '/preview-boundary.json') {
    sendJson(request, response, 200, pathname, config, previewBoundary());
    return;
  }

  let target;
  let root;
  if (OWN_STATIC.has(pathname)) {
    target = OWN_STATIC.get(pathname);
    root = HERE;
  } else if (allowedPublicPath(pathname)) {
    target = resolve(config.publicRoot, pathname.slice(1));
    root = config.publicRoot;
  } else {
    sendJson(request, response, 404, '/error', config, { error: 'not-found' });
    return;
  }

  try {
    const bytes = await readRegularFile(target, root);
    verifyContentAddressedComponent(pathname, bytes);
    const immutable = pathname.includes('/components/') ? 'public, max-age=31536000, immutable' : 'no-store';
    sendBytes(request, response, 200, pathname, config, bytes, MIME.get(extension(pathname)) || 'application/octet-stream', immutable);
  } catch {
    sendJson(request, response, 404, '/error', config, { error: 'not-found' });
  }
}

export function createPreviewServer(config, options = {}) {
  const audit = {
    store: options.auditStore || new DurableInvalidBootstrapAudit({ path: config.auditPath }),
    ready: null,
  };
  audit.ready = audit.store.initialize().catch(error => {
    audit.error = error;
  });
  const server = createServer((request, response) => {
    void audit.ready.then(() => serve(request, response, config, audit)).catch(() => {
      if (!response.headersSent) sendJson(request, response, 500, '/error', config, { error: 'internal' });
      else response.destroy();
    });
  });
  server.vibappAudit = audit;
  return server;
}

export async function startPreviewServer(config = loadConfig()) {
  const server = createPreviewServer(config);
  await server.vibappAudit.ready;
  await new Promise((resolveListen, rejectListen) => {
    server.once('error', rejectListen);
    server.listen(config.port, config.listenHost, () => {
      server.off('error', rejectListen);
      resolveListen();
    });
  });
  return server;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const config = loadConfig();
  const server = await startPreviewServer(config);
  process.stdout.write(JSON.stringify({
    status: 'listening',
    listen: config.listenHost + ':' + config.port,
    preview_origin: config.previewOrigin,
    parent_origin: config.parentOrigin,
    invalid_bootstrap_audit: server.vibappAudit.store.status(),
    ...previewBoundary(),
  }) + '\n');
  for (const signal of ['SIGINT', 'SIGTERM']) {
    process.once(signal, () => server.close(() => process.exit(0)));
  }
}
