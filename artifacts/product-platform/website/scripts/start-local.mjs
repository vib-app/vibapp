#!/usr/bin/env node

import { spawn } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { lstat } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { assertBuiltPreviewOrigin, LOCAL_PREVIEW_ORIGIN } from './preview-config.mjs';
import { computeDesktopBuildInputs } from '../../../desktop/scripts/desktop-build-inputs.mjs';

const websiteRoot = fileURLToPath(new URL('../', import.meta.url));
const vinextCli = fileURLToPath(new URL('../node_modules/vinext/dist/cli.js', import.meta.url));
const productBackend = fileURLToPath(new URL('../../../web-product-backend/server.mjs', import.meta.url));
const defaultProductBridge = fileURLToPath(new URL('../../../desktop/target/release/vibapp-product-bridge', import.meta.url));
const defaultProductData = fileURLToPath(new URL('../output/local-product-data/', import.meta.url));
const forwardedArgs = process.argv.slice(2);
const productPort = Number(process.env.VIBAPP_WEB_PRODUCT_PORT || '3189');
const productOrigin = `http://127.0.0.1:${productPort}`;
const expectedBuildInputs = await computeDesktopBuildInputs();
const fixedEnvironment = {
  NODE_ENV: 'production',
  VIBAPP_PREVIEW_LOCAL: '1',
  VIBAPP_PREVIEW_ORIGIN: LOCAL_PREVIEW_ORIGIN,
  VIBAPP_PRODUCT_BACKEND_URL: productOrigin,
};

if (forwardedArgs.length === 1 && forwardedArgs[0] === '--print-config') {
  process.stdout.write(JSON.stringify({
    command: [process.execPath, vinextCli, 'start', '--hostname', '127.0.0.1'],
    environment: fixedEnvironment,
    productBackend: {
      origin: productOrigin,
      bridge: process.env.VIBAPP_PRODUCT_BRIDGE_BIN || defaultProductBridge,
      token: 'generated-at-startup-not-printed',
      expectedBuildInputs,
    },
  }) + '\n');
  process.exit(0);
}

assertBuiltPreviewOrigin(websiteRoot, LOCAL_PREVIEW_ORIGIN);
if (!Number.isInteger(productPort) || productPort < 1 || productPort > 65535) {
  throw new Error('VIBAPP_WEB_PRODUCT_PORT must be a valid TCP port');
}
const bridge = process.env.VIBAPP_PRODUCT_BRIDGE_BIN || defaultProductBridge;
const bridgeMetadata = await lstat(bridge).catch(() => null);
if (!bridgeMetadata?.isFile() || bridgeMetadata.isSymbolicLink() || (bridgeMetadata.mode & 0o111) === 0) {
  throw new Error(`VibApp product bridge is unavailable: ${bridge}. Build the release bridge or set VIBAPP_PRODUCT_BRIDGE_BIN.`);
}
const productToken = randomBytes(32).toString('hex');
const backend = spawn(process.execPath, [productBackend], {
  cwd: websiteRoot,
  env: {
    ...process.env,
    VIBAPP_PRODUCT_BRIDGE_BIN: bridge,
    VIBAPP_WEB_PRODUCT_DATA_DIR: process.env.VIBAPP_WEB_PRODUCT_DATA_DIR || defaultProductData,
    VIBAPP_WEB_PRODUCT_TOKEN: productToken,
    VIBAPP_WEB_PRODUCT_PORT: String(productPort),
    VIBAPP_EXPECTED_DESKTOP_BUILD_INPUTS_SHA256: expectedBuildInputs.sha256,
  },
  stdio: ['ignore', 'inherit', 'inherit'],
});

const backendDeadline = Date.now() + 5_000;
while (Date.now() < backendDeadline) {
  if (backend.exitCode !== null) throw new Error(`VibApp product backend exited with ${backend.exitCode}`);
  try {
    const health = await fetch(`${productOrigin}/healthz`, { cache: 'no-store' });
    if (health.ok) break;
  } catch {
    // The bounded loop only covers local process startup.
  }
  await new Promise(resolve => setTimeout(resolve, 50));
}
if (Date.now() >= backendDeadline) {
  backend.kill('SIGTERM');
  throw new Error('VibApp product backend did not become ready within 5 seconds');
}
const child = spawn(
  process.execPath,
  [vinextCli, 'start', ...forwardedArgs, '--hostname', '127.0.0.1'],
  {
    cwd: websiteRoot,
    env: {
      ...process.env,
      ...fixedEnvironment,
      VIBAPP_PRODUCT_BACKEND_TOKEN: productToken,
    },
    stdio: 'inherit',
  },
);

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => {
    child.kill(signal);
    backend.kill(signal);
  });
}

child.once('error', error => {
  console.error(`Unable to start the local Website: ${error.message}`);
  process.exit(1);
});

child.once('exit', (code, signal) => {
  backend.kill('SIGTERM');
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code ?? 1);
});

backend.once('exit', (code, signal) => {
  if (child.exitCode === null) child.kill('SIGTERM');
  if (signal !== 'SIGTERM' && code !== 0) {
    console.error(`VibApp product backend exited unexpectedly (${code ?? signal}).`);
  }
});
