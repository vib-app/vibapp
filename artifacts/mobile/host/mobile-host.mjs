import { mobileProduct, validateProductRequest, validateStorageRequest } from './mobile-protocol.mjs';

const launcher = document.querySelector('#launcher');
let activePort;
launcher.addEventListener('load', () => {
  activePort?.close();
  const channel = new MessageChannel();
  activePort = channel.port1;
  channel.port1.onmessage = event => {
    const request = event.data;
    const storage = validateStorageRequest(request);
    if (!storage && !validateProductRequest(request)) return;
    let value = null;
    let error = null;
    try {
      if (storage) {
        if (request.kind === 'storage-get') value = localStorage.getItem(request.key);
        else localStorage.setItem(request.key, request.value);
      } else value = mobileProduct(request.command);
    } catch (failure) {
      error = storage ? 'Mobile storage is unavailable.' : failure.message;
    }
    channel.port1.postMessage({
      schema_version: storage ? 'vibapp.web-parent-storage-result.experimental-v1' : 'vibapp.web-parent-product-result.experimental-v1',
      kind: storage ? 'storage-result' : 'product-result',
      request_id: request.request_id, value, error,
    });
  };
  channel.port1.start();
  launcher.contentWindow.postMessage({ schema_version: 'vibapp.web-storage-bind.experimental-v1', kind: 'bind-web-storage' }, location.origin, [channel.port2]);
});
addEventListener('pagehide', () => { activePort?.close(); activePort = null; });
