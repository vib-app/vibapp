#!/usr/bin/env node

import { spawn } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { PRODUCTION_PREVIEW_ORIGIN } from './preview-config.mjs';

const websiteRoot = fileURLToPath(new URL('../', import.meta.url));
const preparedGui = existsSync(new URL('../.openai/prepared-gui.json', import.meta.url));
const preparedConfig = preparedGui ? JSON.parse(readFileSync(new URL('../.openai/prepared-gui.json', import.meta.url), 'utf8')) : null;
const vinextCli = fileURLToPath(new URL('../node_modules/vinext/dist/cli.js', import.meta.url));
const child = spawn(process.execPath, [vinextCli, 'build'], {
  cwd: websiteRoot,
  env: {
    ...process.env,
    NODE_ENV: 'production',
    VIBAPP_PREVIEW_LOCAL: '0',
    VIBAPP_PREVIEW_ORIGIN: PRODUCTION_PREVIEW_ORIGIN,
    ...(preparedGui ? { NEXT_PUBLIC_VIBAPP_HOSTED_SHELL: '1', NEXT_PUBLIC_SITE_ORIGIN: preparedConfig.site_origin } : {}),
  },
  stdio: 'inherit',
});

child.once('error', error => {
  console.error(`Unable to build the production Website: ${error.message}`);
  process.exit(1);
});
child.once('exit', code => process.exit(code ?? 1));
