import {
  classifyParentBindingFailure,
  previewBoundary,
  validateExactOrigin,
  validateGuiCacheVersion,
  validateParentBinding,
  validateToken,
} from './preview-protocol.mjs';
import { reportInvalidBootstrap } from '/launcher/invalid-bootstrap-audit.mjs';

const documentRoot = document.documentElement;
const frame = document.querySelector('[data-preview-launcher]');
const status = document.querySelector('[data-preview-status]');
const expectedParentOrigin = validateExactOrigin(documentRoot.dataset.parentOrigin, 'configured-parent-origin');
const expectedPreviewOrigin = validateExactOrigin(location.origin, 'current-preview-origin');
const guiCacheVersion = validateGuiCacheVersion(documentRoot.dataset.guiCacheVersion);
const fragment = new URLSearchParams(location.hash.slice(1));
let expectedNonce;

try {
  expectedNonce = validateToken(fragment.get('nonce'), 'fragment-nonce');
} catch {
  status.textContent = '预览没有有效的一次性会话，应用未启动。';
  throw new Error('preview-session-failed-closed');
}

history.replaceState(null, '', location.pathname + location.search);

let consumed = false;
function bindParent(event) {
  if (consumed) {
    reportInvalidBootstrap('replay', 'preview-frame');
    return;
  }
  const context = {
    eventOrigin: event.origin,
    expectedParentOrigin,
    expectedPreviewOrigin,
    expectedNonce,
    portCount: event.ports.length,
    sourceIsParent: event.source === parent,
  };
  try {
    validateParentBinding(event.data, context);
  } catch {
    reportInvalidBootstrap(classifyParentBindingFailure(event.data, context), 'preview-frame');
    return;
  }

  consumed = true;
  const port = event.ports[0];
  const boundary = previewBoundary();
  port.postMessage({
    ...boundary,
    schema_version: 'vibapp.preview-frame-ready.experimental-v1',
    kind: 'preview-frame-ready',
    channel_id: event.data.channel_id,
    nonce: expectedNonce,
    preview_origin: expectedPreviewOrigin,
  });

  const appId = new URL(location.href).searchParams.get('app');
  const launcherUrl = new URL('/launcher/index.html', expectedPreviewOrigin);
  launcherUrl.searchParams.set('v', guiCacheVersion);
  if (appId && /^[a-z0-9]+(?:[.-][a-z0-9]+)+$/.test(appId)) launcherUrl.searchParams.set('app', appId);
  frame.addEventListener('load', () => {
    frame.contentWindow.postMessage({
      schema_version: 'vibapp.web-storage-bind.experimental-v1',
      kind: 'bind-web-storage',
    }, expectedPreviewOrigin, [port]);
  }, { once: true });
  frame.src = launcherUrl.href;
  status.textContent = '隔离来源已绑定；当前组件执行仍是部分隔离预览。';
}

addEventListener('message', bindParent);
