#!/usr/bin/env node

import { createHash } from 'node:crypto';
import { lstat, readFile, readdir } from 'node:fs/promises';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const DEFAULT_DESKTOP_ROOT = resolve(HERE, '..');
const UNSAFE_NAME = /[\0\r\n\t]/;

function assertInside(root, path, label) {
  const suffix = relative(root, path);
  if (suffix === '' || suffix === '..' || suffix.startsWith(`..${sep}`)) {
    throw new Error(`desktop build input escapes ${label}`);
  }
}

async function addFile(entries, root, label, path) {
  if (!label || UNSAFE_NAME.test(label)) throw new Error(`desktop build input label is unsafe: ${label}`);
  assertInside(root, path, label);
  const metadata = await lstat(path);
  if (!metadata.isFile() || metadata.isSymbolicLink()) {
    throw new Error(`desktop build input is not an ordinary file: ${path}`);
  }
  entries.push({ label, path, size: metadata.size });
}

async function addTree(entries, approvedRoot, root, labelRoot) {
  assertInside(approvedRoot, root, labelRoot);
  const metadata = await lstat(root);
  if (!metadata.isDirectory() || metadata.isSymbolicLink()) {
    throw new Error(`desktop build input root is not an ordinary directory: ${root}`);
  }
  const children = await readdir(root, { withFileTypes: true });
  children.sort((left, right) => Buffer.compare(Buffer.from(left.name), Buffer.from(right.name)));
  for (const child of children) {
    if (!child.name || UNSAFE_NAME.test(child.name)) {
      throw new Error(`desktop build input name is unsafe: ${child.name}`);
    }
    const path = join(root, child.name);
    const label = `${labelRoot}/${child.name}`;
    const current = await lstat(path);
    if (current.isSymbolicLink()) throw new Error(`desktop build input must not be a symlink: ${path}`);
    if (current.isDirectory()) await addTree(entries, approvedRoot, path, label);
    else if (current.isFile()) await addFile(entries, approvedRoot, label, path);
    else throw new Error(`desktop build input is not an ordinary file or directory: ${path}`);
  }
}

export async function desktopBuildInputs(desktopRoot = DEFAULT_DESKTOP_ROOT) {
  desktopRoot = resolve(desktopRoot);
  const artifactsRoot = resolve(desktopRoot, '..');
  const projectRoot = resolve(artifactsRoot, '..');
  const appBuilderRoot = join(artifactsRoot, 'app-builder');
  const cloudAgentRoot = join(artifactsRoot, 'cloud-agent');
  const codeagentAdapterRoot = join(artifactsRoot, 'codeagent-adapter');
  const orchestratorRoot = join(artifactsRoot, 'orchestrator');
  const entries = [];
  const fixed = [
    ['registry-store/public_app_download.py', join(artifactsRoot, 'registry-store/public_app_download.py')],
    ['registry-store/local_appstore.py', join(artifactsRoot, 'registry-store/local_appstore.py')],
    ['desktop/packaging/macos/Info.plist', join(desktopRoot, 'packaging/macos/Info.plist')],
    ['app-builder/app_builder.py', join(appBuilderRoot, 'app_builder.py')],
    ['app-builder/common.py', join(appBuilderRoot, 'common.py')],
    ['app-builder/descriptor_reconciliation.py', join(appBuilderRoot, 'descriptor_reconciliation.py')],
    ['app-builder/schemas/candidate.schema.json', join(appBuilderRoot, 'schemas/candidate.schema.json')],
    ['app-builder/schemas/quarantine-receipt.schema.json', join(appBuilderRoot, 'schemas/quarantine-receipt.schema.json')],
    ['app-builder/schemas/verifier-decision.schema.json', join(appBuilderRoot, 'schemas/verifier-decision.schema.json')],
    ['app-builder/verifier.py', join(appBuilderRoot, 'verifier.py')],
    ['cloud-agent/cloud_agent.py', join(cloudAgentRoot, 'cloud_agent.py')],
    ['cloud-agent/skills/vibapp-ui-ux/SKILL.md', join(cloudAgentRoot, 'skills/vibapp-ui-ux/SKILL.md')],
    ['cloud-agent/fixtures/dry-run/provider-result.json', join(cloudAgentRoot, 'fixtures/dry-run/provider-result.json')],
    ['cloud-agent/fixtures/dry-run/source/Cargo.toml', join(cloudAgentRoot, 'fixtures/dry-run/source/Cargo.toml')],
    ['cloud-agent/fixtures/dry-run/source/src/lib.rs', join(cloudAgentRoot, 'fixtures/dry-run/source/src/lib.rs')],
    ['cloud-agent/schemas/cloud-codeagent-task.experimental-v2.schema.json', join(cloudAgentRoot, 'schemas/cloud-codeagent-task.experimental-v2.schema.json')],
    ['cloud-agent/schemas/cloud-codeagent-task.schema.json', join(cloudAgentRoot, 'schemas/cloud-codeagent-task.schema.json')],
    ['cloud-agent/schemas/provider-result.schema.json', join(cloudAgentRoot, 'schemas/provider-result.schema.json')],
    ['cloud-agent/schemas/source-handoff.schema.json', join(cloudAgentRoot, 'schemas/source-handoff.schema.json')],
    ['cloud-agent/starter/README.md', join(cloudAgentRoot, 'starter/README.md')],
    ['cloud-agent/starter/opencode_instructions.txt', join(cloudAgentRoot, 'starter/opencode_instructions.txt')],
    ['cloud-agent/starter/codex_instructions.txt', join(cloudAgentRoot, 'starter/codex_instructions.txt')],
    ['cloud-agent/starter/vibapp_support.rs', join(cloudAgentRoot, 'starter/vibapp_support.rs')],
    ['codeagent-adapter/codeagent_adapter.py', join(codeagentAdapterRoot, 'codeagent_adapter.py')],
    ['codeagent-adapter/docker_provider.py', join(codeagentAdapterRoot, 'docker_provider.py')],
    ['codeagent-launcher/codeagent_launcher.py', join(artifactsRoot, 'codeagent-launcher/codeagent_launcher.py')],
    ['codeagent-launcher/docker/entry.mjs', join(artifactsRoot, 'codeagent-launcher/docker/entry.mjs')],
    ['codeagent-launcher/docker/opencode-provider.mjs', join(artifactsRoot, 'codeagent-launcher/docker/opencode-provider.mjs')],
    ['codeagent-launcher/docker_executor.py', join(artifactsRoot, 'codeagent-launcher/docker_executor.py')],
    ['codeagent-launcher/host_budget.py', join(artifactsRoot, 'codeagent-launcher/host_budget.py')],
    ['desktop/fixtures/state.json', join(desktopRoot, 'fixtures/state.json')],
    ['desktop/runtime-apps/hello/component.wasm', join(desktopRoot, 'runtime-apps/hello/component.wasm')],
    ['desktop/src-tauri/Cargo.lock', join(desktopRoot, 'src-tauri/Cargo.lock')],
    ['desktop/src-tauri/Cargo.toml', join(desktopRoot, 'src-tauri/Cargo.toml')],
    ['desktop/src-tauri/build.rs', join(desktopRoot, 'src-tauri/build.rs')],
    ['desktop/src-tauri/tauri.conf.json', join(desktopRoot, 'src-tauri/tauri.conf.json')],
    ['orchestrator/delivery_controller.py', join(orchestratorRoot, 'delivery_controller.py')],
    ['orchestrator/delivery_history.py', join(orchestratorRoot, 'delivery_history.py')],
    ['orchestrator/runtime_readiness.py', join(orchestratorRoot, 'runtime_readiness.py')],
    ['orchestrator/task_archive.py', join(orchestratorRoot, 'task_archive.py')],
    ['orchestrator/dry_run_adapter.py', join(orchestratorRoot, 'dry_run_adapter.py')],
    ['orchestrator/orchestrator.py', join(orchestratorRoot, 'orchestrator.py')],
    ['wit/experimental-v0/contract.wit', join(projectRoot, 'wit/experimental-v0/contract.wit')],
  ];
  for (const [label, path] of fixed) await addFile(entries, projectRoot, label, path);
  await addTree(entries, projectRoot, join(desktopRoot, 'src-tauri/src'), 'desktop/src-tauri/src');
  await addTree(entries, projectRoot, join(desktopRoot, 'ui'), 'desktop/ui');
  entries.sort((left, right) => Buffer.compare(Buffer.from(left.label), Buffer.from(right.label)));
  if (new Set(entries.map(entry => entry.label)).size !== entries.length) {
    throw new Error('desktop build input labels are not unique');
  }
  return entries;
}

export async function computeDesktopBuildInputs(desktopRoot = DEFAULT_DESKTOP_ROOT) {
  const entries = await desktopBuildInputs(desktopRoot);
  const aggregate = createHash('sha256');
  for (const entry of entries) {
    const bytes = await readFile(entry.path);
    if (bytes.byteLength !== entry.size) throw new Error(`desktop build input changed while reading: ${entry.path}`);
    aggregate.update(Buffer.from(entry.label));
    aggregate.update(Buffer.from([0]));
    aggregate.update(createHash('sha256').update(bytes).digest('hex'));
    aggregate.update(Buffer.from([0]));
    aggregate.update(String(bytes.byteLength));
    aggregate.update(Buffer.from([0]));
  }
  return {
    schema_version: 'vibapp.desktop-build-inputs.v1',
    sha256: aggregate.digest('hex'),
    file_count: entries.length,
  };
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  process.stdout.write(`${JSON.stringify(await computeDesktopBuildInputs())}\n`);
}
