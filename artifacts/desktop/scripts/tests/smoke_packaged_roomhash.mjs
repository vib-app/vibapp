#!/usr/bin/env node

import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const DESKTOP_ROOT = resolve(HERE, '../..');
const BUNDLE = resolve(process.argv[2] || join(DESKTOP_ROOT, 'dist/VibApp.app'));
const NODE = join(BUNDLE, 'Contents/Resources/roomhash/node');
const HOST = join(BUNDLE, 'Contents/Resources/roomhash/roomhash-host.mjs');
const ROOMHASH_ROOT = join(BUNDLE, 'Contents/Resources/roomhash/current');
const COLLABORATION_ROOT = join(BUNDLE, 'Contents/Resources/roomhash/collaboration');
const CANDIDATE_ROOT = resolve(DESKTOP_ROOT, '../app-builder/demo-output/pipeline/candidates');
const scratch = await mkdtemp(join(tmpdir(), 'vibapp-packaged-roomhash-'));

let child;
let stderr = '';
try {
  child = spawn(NODE, [HOST], {
    stdio: ['pipe', 'pipe', 'pipe'],
    env: {
      PATH: process.env.PATH || '',
      VIBAPP_ROOMHASH_ROOT: ROOMHASH_ROOT,
      VIBAPP_ROOMHASH_COLLABORATION_ROOT: COLLABORATION_ROOT,
      VIBAPP_ROOMHASH_DATA_DIR: scratch,
      VIBAPP_ROOMHASH_ALLOWED_SEED_ROOTS: CANDIDATE_ROOT,
    },
  });
  child.stderr.setEncoding('utf8');
  child.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-8192); });

  const lines = createInterface({ input: child.stdout });
  const buffered = [];
  const waiting = [];
  lines.on('line', line => {
    const value = JSON.parse(line);
    const waiter = waiting.shift();
    if (waiter) waiter.resolve(value);
    else buffered.push(value);
  });
  child.once('error', error => {
    const waiter = waiting.shift();
    if (waiter) waiter.reject(error);
  });

  const next = (timeoutMs = 30_000) => new Promise((resolveNext, rejectNext) => {
    if (buffered.length) return resolveNext(buffered.shift());
    const timeout = setTimeout(() => rejectNext(new Error(`packaged host timed out: ${stderr}`)), timeoutMs);
    waiting.push({
      resolve(value) {
        clearTimeout(timeout);
        resolveNext(value);
      },
      reject(error) {
        clearTimeout(timeout);
        rejectNext(error);
      },
    });
  });

  let requestId = 0;
  async function send(command, args = {}) {
    requestId += 1;
    child.stdin.write(`${JSON.stringify({
      schema: 'vibapp.roomhash-host-request.experimental-v1',
      request_id: requestId,
      command,
      args,
    })}\n`);
    return next();
  }

  const ready = await next();
  assert.equal(ready.ok, true, JSON.stringify(ready));
  assert.deepEqual(ready.result, { status: 'ready' });
  const configured = await send('configure', {
    upload_limit_bps: 1024 * 1024,
    download_limit_bps: 4 * 1024 * 1024,
    max_conns: 4,
    dht: false,
    lsd: false,
    pex: false,
    tracker_urls: [],
    turn: null,
  });
  assert.equal(configured.ok, true, JSON.stringify(configured));
  const started = await send('start');
  assert.equal(started.ok, true, JSON.stringify(started));
  assert.equal(started.result.implementation, 'roomhash-current-headless-webtorrent-3.0.16');
  assert.equal(started.result.collaboration.available, true, JSON.stringify(started));
  const status = await send('status');
  assert.equal(status.ok, true, JSON.stringify(status));
  assert.equal(status.result.status, 'running');
  assert.equal(status.result.active_transfers, 0);
  const stopped = await send('shutdown');
  assert.equal(stopped.ok, true, JSON.stringify(stopped));
  assert.deepEqual(stopped.result, { status: 'stopped' });

  child.stdin.end();
  const exitCode = await new Promise((resolveExit, rejectExit) => {
    if (child.exitCode !== null) return resolveExit(child.exitCode);
    child.once('error', rejectExit);
    child.once('exit', resolveExit);
  });
  assert.equal(exitCode, 0, stderr);
  console.log(JSON.stringify({
    ok: true,
    bundle: BUNDLE,
    implementation: started.result.implementation,
    collaboration: started.result.collaboration.state,
  }));
} finally {
  if (child && child.exitCode === null) child.kill('SIGKILL');
  await rm(scratch, { recursive: true, force: true });
}
