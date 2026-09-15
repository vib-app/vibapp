import { readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

export const LOCAL_PREVIEW_ORIGIN = 'http://127.0.0.1:4174';
export const PRODUCTION_PREVIEW_ORIGIN = 'https://preview.vibapp.ai';

export function assertBuiltPreviewOrigin(websiteRoot, expectedOrigin) {
  const serverBundle = join(websiteRoot, 'dist', 'server', 'index.js');
  let size;
  try {
    size = statSync(serverBundle).size;
  } catch {
    throw new Error('Website build is missing; run the matching build command before start');
  }
  if (size < 1 || size > 64 * 1024 * 1024) {
    throw new Error('Website server bundle is outside the bounded startup policy');
  }
  const source = readFileSync(serverBundle, 'utf8');
  if (!source.includes(`frame-src ${expectedOrigin}; object-src`)) {
    const command = expectedOrigin === LOCAL_PREVIEW_ORIGIN ? 'npm run build:local' : 'npm run build';
    throw new Error(`Website build preview origin does not match startup policy; run ${command}`);
  }
}
