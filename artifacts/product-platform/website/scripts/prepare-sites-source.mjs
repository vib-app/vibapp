import { createHash } from 'node:crypto';
import { cp, lstat, mkdir, readdir, readFile, writeFile } from 'node:fs/promises';
import { isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { HOSTED_SHELL_RESOURCE_CSP, prepareHostedShellHtml } from './hosted-shell-html.mjs';

const source = fileURLToPath(new URL('../', import.meta.url));
const [destination, siteOrigin] = process.argv.slice(2);
if (!destination || !isAbsolute(destination) || !siteOrigin || new URL(siteOrigin).protocol !== 'https:'
  || new URL(siteOrigin).origin !== siteOrigin) throw new Error('Expected empty absolute destination and exact HTTPS Site origin');
const target = resolve(destination);
await mkdir(target, { recursive: true });
if ((await readdir(target)).length) throw new Error('Destination must be empty; preserve previous publishing checkouts');
const registry = JSON.parse(await readFile(join(source, 'public/data/registry.snapshot.json'), 'utf8'));
if (registry.consumer_notes?.website_projection_mode !== 'production-public-only'
  || registry.browser_preview_records?.length || registry.consumer_notes?.local_private_preview_enabled !== false) {
  throw new Error('Run the production GUI sync first; never publish local preview state');
}
// Explicit source-only allowlist. Never copy .env, local provider settings, logs,
// the parent Git repository, build caches, or the private product backend.
for (const path of ['app', 'lib', 'scripts', 'tests', 'package.json', 'package-lock.json',
  'vite.config.ts', 'next.config.ts', 'tsconfig.json', 'eslint.config.mjs', 'README.md', 'public']) {
  await cp(join(source, path), join(target, path), { recursive: true, dereference: false, filter: async path => {
    if ((await lstat(path)).isSymbolicLink()) throw new Error('Symlink is not a publishable source input');
    return true;
  } });
}
await mkdir(join(target, '.openai'));
await cp(join(source, '.openai/hosting.json'), join(target, '.openai/hosting.json'));
const htmlPath = join(target, 'public/launcher/index.html');
const html = prepareHostedShellHtml(await readFile(htmlPath, 'utf8'));
await writeFile(htmlPath, html);
const guiManifestPath = join(target, 'public/launcher/gui-sync-manifest.json');
const guiManifest = JSON.parse(await readFile(guiManifestPath, 'utf8'));
guiManifest.adapter.hosted_shell_only = true;
guiManifest.files['index.html'].generated_sha256 = createHash('sha256').update(html).digest('hex');
await writeFile(guiManifestPath, JSON.stringify(guiManifest, null, 2) + '\n');
// Cloudflare serves static assets before the application Worker. Asset CSP must
// therefore live in _headers too; framework headers alone cannot guard guests.
await writeFile(join(target, 'public/_headers'), `/launcher/*
  Content-Security-Policy: ${HOSTED_SHELL_RESOURCE_CSP}; frame-ancestors 'self'
  X-Content-Type-Options: nosniff
  Cache-Control: no-cache
/data/*
  Cache-Control: no-cache
`);
const files = {};
async function inventory(relative) {
  for (const entry of (await readdir(join(target, relative), { withFileTypes: true })).sort((a, b) => a.name.localeCompare(b.name))) {
    const path = relative + '/' + entry.name;
    if (entry.isDirectory()) await inventory(path);
    else if (entry.isFile()) files[path] = createHash('sha256').update(await readFile(join(target, path))).digest('hex');
    else throw new Error('Non-regular public asset');
  }
}
await inventory('public');
await writeFile(join(target, '.openai/prepared-gui.json'), JSON.stringify({
  schema_version: 'vibapp.sites-gui-snapshot.v1', site_origin: siteOrigin,
  shared_gui_source: 'artifacts/desktop/ui', guest_execution_enabled: true,
  guest_execution_policy: 'verified-public-browser-bindings-only',
  remote_service_connected: false, files,
}, null, 2) + '\n');
await writeFile(join(target, '.gitignore'), 'node_modules/\ndist/\n.next/\n.wrangler/\n.env*\n*.tsbuildinfo\n.openai/workflow*.json*\n.openai/execution*.json\n');
console.log(JSON.stringify({ destination: target, public_records: registry.records.length, assets: Object.keys(files).length, private_previews: 0 }));
