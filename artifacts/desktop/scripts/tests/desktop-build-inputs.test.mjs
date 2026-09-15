import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, rm, symlink, unlink, utimes, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { computeDesktopBuildInputs, desktopBuildInputs } from '../desktop-build-inputs.mjs';

const desktopRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const bridgeNames = ['entry.mjs', 'opencode-provider.mjs'];

test('trusted image bridge inputs are explicit in Node, Rust and packaging inventories', async () => {
  const inputs = await desktopBuildInputs(desktopRoot);
  const rust = await readFile(join(desktopRoot, 'src-tauri/build.rs'), 'utf8');
  const shell = await readFile(join(desktopRoot, 'scripts/package-macos.sh'), 'utf8');
  const receiptInputs = shell.split('emit_desktop_build_input_entries() {')[1].split('\n}')[0];
  const preflight = shell.split('for required_file in ')[1].split('; do')[0];
  for (const name of bridgeNames) {
    const label = `codeagent-launcher/docker/${name}`;
    assert.equal(inputs.filter(input => input.label === label).length, 1);
    assert.ok(rust.includes(`"${label}"`), `${label} must be bound into both binary receipts`);
    assert.ok(receiptInputs.includes(`"$codeagent_launcher_root/docker/${name}"`));
    assert.ok(preflight.includes(`"$codeagent_launcher_root/docker/${name}"`));
  }
});

test('packaging copies the exact trusted bridges into the image identity lookup directory', async t => {
  const root = await mkdtemp(join(tmpdir(), 'vibapp-bridge-staging-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const launcherRoot = join(root, 'codeagent-launcher');
  const contents = join(root, 'VibApp.app/Contents');
  await mkdir(join(launcherRoot, 'docker'), { recursive: true });
  const shell = await readFile(join(desktopRoot, 'scripts/package-macos.sh'), 'utf8');
  assert.ok(shell.includes('"$contents/Resources/codeagent-launcher/docker" \\'));
  await mkdir(join(contents, 'Resources/codeagent-launcher/docker'), { recursive: true });
  for (const name of bridgeNames) {
    const bytes = Buffer.from(`// trusted ${name}\n`);
    await writeFile(join(launcherRoot, 'docker', name), bytes);
    const copy = `cp "$codeagent_launcher_root/docker/${name}" "$contents/Resources/codeagent-launcher/docker/${name}"`;
    assert.ok(shell.split('\n').includes(copy));
    const result = spawnSync('/bin/sh', ['-eu', '-c', copy], {
      env: { PATH: '/usr/bin:/bin', codeagent_launcher_root: launcherRoot, contents },
      encoding: 'utf8', timeout: 5000,
    });
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(await readFile(join(contents, 'Resources/codeagent-launcher/docker', name)), bytes);
  }
});

test('bridge byte changes invalidate receipts despite old mtimes; missing and linked bridges fail closed', async t => {
  const root = await mkdtemp(join(tmpdir(), 'vibapp-bridge-receipt-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const fixtureDesktop = join(root, 'artifacts/desktop');
  for (const { label } of await desktopBuildInputs(desktopRoot)) {
    const file = join(root, label.startsWith('wit/') ? label : `artifacts/${label}`);
    await mkdir(dirname(file), { recursive: true });
    await writeFile(file, 'trusted input\n');
  }
  const receipt = await computeDesktopBuildInputs(fixtureDesktop);
  for (const name of bridgeNames) {
    const file = join(root, 'artifacts/codeagent-launcher/docker', name);
    await writeFile(file, 'changed input\n');
    await utimes(file, 1, 1);
    assert.notEqual((await computeDesktopBuildInputs(fixtureDesktop)).sha256, receipt.sha256, name);
    await unlink(file);
    await assert.rejects(computeDesktopBuildInputs(fixtureDesktop), /ENOENT/);
    await symlink(join(fixtureDesktop, 'fixtures/state.json'), file);
    await assert.rejects(computeDesktopBuildInputs(fixtureDesktop), /not an ordinary file/);
    await unlink(file);
    await writeFile(file, 'trusted input\n');
    assert.deepEqual(await computeDesktopBuildInputs(fixtureDesktop), receipt);
  }
});

test('UI/UX skill has one shared receipt label and is staged byte-for-byte', async t => {
  const label = 'cloud-agent/skills/vibapp-ui-ux/SKILL.md';
  const inputs = await desktopBuildInputs(desktopRoot);
  assert.equal(inputs.filter(input => input.label === label).length, 1);
  const rust = await readFile(join(desktopRoot, 'src-tauri/build.rs'), 'utf8');
  const shell = await readFile(join(desktopRoot, 'scripts/package-macos.sh'), 'utf8');
  assert.ok(rust.includes(`"${label}"`));
  const input = '"$cloud_agent_root/skills/vibapp-ui-ux/SKILL.md"';
  assert.ok(shell.split('emit_desktop_build_input_entries() {')[1].split('\n}')[0].includes(input));
  assert.ok(shell.split('for required_file in ')[1].split('; do')[0].includes(input));
  const root = await mkdtemp(join(tmpdir(), 'vibapp-skill-staging-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const contents = join(root, 'VibApp.app/Contents');
  await mkdir(join(contents, 'Resources/cloud-agent/skills/vibapp-ui-ux'), { recursive: true });
  const copy = 'cp "$cloud_agent_root/skills/vibapp-ui-ux/SKILL.md" "$contents/Resources/cloud-agent/skills/vibapp-ui-ux/SKILL.md"';
  assert.ok(shell.split('\n').includes(copy));
  const result = spawnSync('/bin/sh', ['-eu', '-c', copy], {
    env: { PATH: '/usr/bin:/bin', cloud_agent_root: resolve(desktopRoot, '../cloud-agent'), contents },
    encoding: 'utf8', timeout: 5000,
  });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(await readFile(join(contents, 'Resources', label)),
                   await readFile(join(desktopRoot, '../cloud-agent/skills/vibapp-ui-ux/SKILL.md')));
});

test('skill bytes affect receipts; stale mtimes, omission and links cannot hide a change', async t => {
  const root = await mkdtemp(join(tmpdir(), 'vibapp-skill-receipt-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const fixtureDesktop = join(root, 'artifacts/desktop');
  for (const { label } of await desktopBuildInputs(desktopRoot)) {
    const file = join(root, label.startsWith('wit/') ? label : `artifacts/${label}`);
    await mkdir(dirname(file), { recursive: true });
    await writeFile(file, 'trusted input\n');
  }
  const receipt = await computeDesktopBuildInputs(fixtureDesktop);
  const skill = join(root, 'artifacts/cloud-agent/skills/vibapp-ui-ux/SKILL.md');
  await writeFile(skill, 'changed input\n');
  await utimes(skill, 1, 1);
  assert.notEqual((await computeDesktopBuildInputs(fixtureDesktop)).sha256, receipt.sha256);
  await unlink(skill);
  await assert.rejects(computeDesktopBuildInputs(fixtureDesktop), /ENOENT/);
  await symlink(join(fixtureDesktop, 'fixtures/state.json'), skill);
  await assert.rejects(computeDesktopBuildInputs(fixtureDesktop), /not an ordinary file/);
});
