import { createHash } from 'node:crypto';
import { existsSync } from 'node:fs';
import { lstat, readFile } from 'node:fs/promises';

const prepared = new URL('../.openai/prepared-gui.json', import.meta.url);
if (existsSync(prepared)) {
  // Standalone Sites source carries the exact shared-GUI build inputs, not a
  // second UI implementation. Validate them before building the Worker.
  const snapshot = JSON.parse(await readFile(prepared, 'utf8'));
  if (snapshot.schema_version !== 'vibapp.sites-gui-snapshot.v1'
    || !snapshot.files || Object.keys(snapshot.files).length < 5) throw new Error('Invalid prepared GUI snapshot');
  for (const [path, digest] of Object.entries(snapshot.files)) {
    if (!/^public\/[a-zA-Z0-9_./-]+$/.test(path) || path.split('/').includes('..')) throw new Error('Invalid GUI input path');
    const target = new URL('../' + path, import.meta.url);
    const stat = await lstat(target);
    if (!stat.isFile() || stat.isSymbolicLink()) throw new Error('GUI input must be a regular file');
    const actual = createHash('sha256').update(await readFile(target)).digest('hex');
    if (actual !== digest) throw new Error('Prepared GUI input changed: ' + path);
  }
  const registry = JSON.parse(await readFile(new URL('../public/data/registry.snapshot.json', import.meta.url), 'utf8'));
  if (registry.consumer_notes?.website_projection_mode !== 'production-public-only'
    || registry.browser_preview_records?.length) throw new Error('Private preview data cannot be hosted');
  console.log('Shared GUI snapshot verified; no local backend or provider credentials included.');
} else {
  const { syncWebGui } = await import('../../../web-client-core/sync-web-gui.mjs');
  await syncWebGui();
}
