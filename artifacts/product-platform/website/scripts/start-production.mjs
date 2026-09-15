#!/usr/bin/env node

import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { assertBuiltPreviewOrigin, PRODUCTION_PREVIEW_ORIGIN } from './preview-config.mjs';

const websiteRoot = fileURLToPath(new URL('../', import.meta.url));
const vinextCli = fileURLToPath(new URL('../node_modules/vinext/dist/cli.js', import.meta.url));
const forwardedArgs = process.argv.slice(2);
const fixedEnvironment = {
  NODE_ENV: 'production',
  VIBAPP_PREVIEW_LOCAL: '0',
  VIBAPP_PREVIEW_ORIGIN: PRODUCTION_PREVIEW_ORIGIN,
};

if (forwardedArgs.length === 1 && forwardedArgs[0] === '--print-config') {
  process.stdout.write(JSON.stringify({
    command: [process.execPath, vinextCli, 'start'],
    environment: fixedEnvironment,
  }) + '\n');
  process.exit(0);
}

assertBuiltPreviewOrigin(websiteRoot, PRODUCTION_PREVIEW_ORIGIN);
const child = spawn(process.execPath, [vinextCli, 'start', ...forwardedArgs], {
  cwd: websiteRoot,
  env: { ...process.env, ...fixedEnvironment },
  stdio: 'inherit',
});

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => child.kill(signal));
}

child.once('error', error => {
  console.error(`Unable to start the production Website: ${error.message}`);
  process.exit(1);
});

child.once('exit', (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code ?? 1);
});
