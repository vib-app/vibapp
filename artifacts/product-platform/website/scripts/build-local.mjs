#!/usr/bin/env node

import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { LOCAL_PREVIEW_ORIGIN } from './preview-config.mjs';

const websiteRoot = fileURLToPath(new URL('../', import.meta.url));
const vinextCli = fileURLToPath(new URL('../node_modules/vinext/dist/cli.js', import.meta.url));
const child = spawn(process.execPath, [vinextCli, 'build'], {
  cwd: websiteRoot,
  env: {
    ...process.env,
    NODE_ENV: 'production',
    VIBAPP_PREVIEW_LOCAL: '1',
    VIBAPP_PREVIEW_ORIGIN: LOCAL_PREVIEW_ORIGIN,
  },
  stdio: 'inherit',
});

child.once('error', error => {
  console.error(`Unable to build the local Website: ${error.message}`);
  process.exit(1);
});
child.once('exit', code => process.exit(code ?? 1));
