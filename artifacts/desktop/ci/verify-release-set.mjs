import { readFileSync, readdirSync, writeFileSync, statSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { createHash } from 'node:crypto';
const directory = resolve(process.argv[2] || '.');
const expected = new Set(['macOS-arm64', 'macOS-x64', 'Linux-x64', 'Windows-x64']);
const manifests = readdirSync(directory).filter(name => /^release-manifest-[a-z0-9-]+\.json$/.test(name));
const records = manifests.map(name => {
  const record = JSON.parse(readFileSync(join(directory, name)));
  if (record.schema_version !== 'vibapp.client-release.v1' || record.source_commit !== process.env.GITHUB_SHA || !expected.delete(record.platform)) throw Error('Unexpected or duplicate release platform/source');
  if (!/^VibApp-[A-Za-z0-9.-]+$/.test(record.artifact?.name) || !/^[a-f0-9]{64}$/.test(record.artifact?.sha256)) throw Error('Invalid artifact');
  const path = join(directory, record.artifact.name);
  if (statSync(path).size !== record.artifact.size || createHash('sha256').update(readFileSync(path)).digest('hex') !== record.artifact.sha256) throw Error('Artifact bytes mismatch');
  return record;
});
if (expected.size) throw Error('Missing release platforms: ' + [...expected].join(', '));
writeFileSync(join(directory, 'release-manifest.json'), JSON.stringify({ schema_version: 'vibapp.client-release-set.v1', source_commit: process.env.GITHUB_SHA, clients: records }, null, 2) + '\n');
console.log('Verified all desktop platform artifacts at exact source commit');
