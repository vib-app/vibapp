import { randomUUID } from 'node:crypto';
import {
  lstat,
  mkdir,
  readFile,
  rename,
  unlink,
  writeFile,
} from 'node:fs/promises';
import { dirname, isAbsolute } from 'node:path';

export const INVALID_BOOTSTRAP_REASONS = Object.freeze([
  'extra-authority',
  'replay',
  'tamper',
  'wrong-nonce',
  'wrong-origin',
]);

export const INVALID_BOOTSTRAP_SOURCES = Object.freeze([
  'app-worker',
  'launcher-runtime',
  'preview-frame',
]);

const REASON_SET = new Set(INVALID_BOOTSTRAP_REASONS);
const SOURCE_SET = new Set(INVALID_BOOTSTRAP_SOURCES);
const EVENT_KEYS = [
  'event_id',
  'observed_at_utc',
  'reason',
  'result',
  'schema_version',
  'source',
].join(',');
const EVENT_SCHEMA = 'vibapp.invalid-bootstrap-audit.experimental-v1';
const WHOLE_SECOND_UTC = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;

function canonicalJson(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(canonicalJson).join(',') + ']';
  return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonicalJson(value[key])).join(',') + '}';
}

function wholeSecondUtc(date) {
  return date.toISOString().replace(/\.\d{3}Z$/, 'Z');
}

function auditError(detail) {
  return new Error('invalid-bootstrap-audit:' + detail);
}

function validateEvent(event) {
  if (!event || typeof event !== 'object' || Array.isArray(event)) throw auditError('event-object');
  if (Object.keys(event).sort().join(',') !== EVENT_KEYS) throw auditError('event-keys');
  if (event.schema_version !== EVENT_SCHEMA) throw auditError('event-schema');
  if (typeof event.event_id !== 'string' || !/^audit-[0-9a-f-]{36}$/.test(event.event_id)) throw auditError('event-id');
  if (!WHOLE_SECOND_UTC.test(event.observed_at_utc) || !Number.isFinite(Date.parse(event.observed_at_utc))) throw auditError('event-time');
  if (!REASON_SET.has(event.reason)) throw auditError('event-reason');
  if (!SOURCE_SET.has(event.source)) throw auditError('event-source');
  if (event.result !== 'rejected-before-authority') throw auditError('event-result');
  if (canonicalJson(event).length > 384) throw auditError('event-size');
  return event;
}

async function regularFileOrMissing(path) {
  try {
    const stat = await lstat(path);
    if (!stat.isFile() || stat.isSymbolicLink()) throw auditError('path-not-regular');
    return stat;
  } catch (error) {
    if (error?.code === 'ENOENT') return null;
    throw error;
  }
}

export class DurableInvalidBootstrapAudit {
  constructor({
    path,
    maximumBytes = 64 * 1024,
    maximumEvents = 256,
    retentionMilliseconds = 7 * 24 * 60 * 60 * 1000,
    now = () => new Date(),
    id = () => randomUUID(),
  }) {
    if (typeof path !== 'string' || !isAbsolute(path)) throw auditError('absolute-path-required');
    if (!Number.isInteger(maximumBytes) || maximumBytes < 4096 || maximumBytes > 1024 * 1024) throw auditError('maximum-bytes');
    if (!Number.isInteger(maximumEvents) || maximumEvents < 8 || maximumEvents > 4096) throw auditError('maximum-events');
    if (!Number.isInteger(retentionMilliseconds) || retentionMilliseconds < 60_000 || retentionMilliseconds > 31 * 24 * 60 * 60 * 1000) throw auditError('retention');
    this.path = path;
    this.rotatedPath = path + '.1';
    this.maximumBytes = maximumBytes;
    this.maximumEvents = maximumEvents;
    this.retentionMilliseconds = retentionMilliseconds;
    this.now = now;
    this.id = id;
    this.events = [];
    this.initialized = false;
    this.healthy = false;
    this.recoveredTruncatedTail = false;
    this.queue = Promise.resolve();
  }

  status() {
    return Object.freeze({
      schema_version: 'vibapp.invalid-bootstrap-audit-status.experimental-v1',
      healthy: this.healthy,
      durable: true,
      payload_storage: 'forbidden',
      maximum_bytes_per_file: this.maximumBytes,
      maximum_events_per_file: this.maximumEvents,
      rotation_files: 1,
      retention_seconds: Math.floor(this.retentionMilliseconds / 1000),
      recovered_truncated_tail: this.recoveredTruncatedTail,
    });
  }

  async initialize() {
    if (this.initialized) return this.status();
    await mkdir(dirname(this.path), { recursive: true, mode: 0o700 });
    const stat = await regularFileOrMissing(this.path);
    let bytes = stat ? await readFile(this.path) : Buffer.alloc(0);
    if (bytes.byteLength > this.maximumBytes) throw auditError('file-size-limit');

    if (bytes.byteLength > 0 && bytes.at(-1) !== 0x0a) {
      const newline = bytes.lastIndexOf(0x0a);
      bytes = newline === -1 ? Buffer.alloc(0) : bytes.subarray(0, newline + 1);
      this.recoveredTruncatedTail = true;
    }

    const events = [];
    for (const line of bytes.toString('utf8').split('\n')) {
      if (!line) continue;
      let event;
      try {
        event = JSON.parse(line);
      } catch {
        throw auditError('corrupt-complete-record');
      }
      validateEvent(event);
      if (line !== canonicalJson(event)) throw auditError('noncanonical-record');
      events.push(event);
    }
    if (events.length > this.maximumEvents) throw auditError('event-count-limit');
    const cutoff = this.now().getTime() - this.retentionMilliseconds;
    this.events = events.filter(event => Date.parse(event.observed_at_utc) >= cutoff);

    const rotatedStat = await regularFileOrMissing(this.rotatedPath);
    if (rotatedStat) {
      if (rotatedStat.size > this.maximumBytes) throw auditError('rotated-file-size-limit');
      const rotatedBytes = await readFile(this.rotatedPath);
      if (rotatedBytes.byteLength > 0 && rotatedBytes.at(-1) !== 0x0a) throw auditError('corrupt-rotated-tail');
      const rotatedEvents = [];
      for (const line of rotatedBytes.toString('utf8').split('\n')) {
        if (!line) continue;
        let event;
        try {
          event = JSON.parse(line);
        } catch {
          throw auditError('corrupt-rotated-record');
        }
        validateEvent(event);
        if (line !== canonicalJson(event)) throw auditError('noncanonical-rotated-record');
        rotatedEvents.push(event);
      }
      if (rotatedEvents.length > this.maximumEvents) throw auditError('rotated-event-count-limit');
      if (rotatedEvents.every(event => Date.parse(event.observed_at_utc) < cutoff)) await unlink(this.rotatedPath);
    }
    await this.#writeCurrent();
    this.initialized = true;
    this.healthy = true;
    return this.status();
  }

  async record({ reason, source }) {
    const operation = this.queue.then(async () => {
      if (!this.initialized || !this.healthy) throw auditError('unavailable');
      if (!REASON_SET.has(reason)) throw auditError('reason');
      if (!SOURCE_SET.has(source)) throw auditError('source');
      const now = this.now();
      const cutoff = now.getTime() - this.retentionMilliseconds;
      this.events = this.events.filter(event => Date.parse(event.observed_at_utc) >= cutoff);
      const event = validateEvent({
        schema_version: EVENT_SCHEMA,
        event_id: 'audit-' + this.id(),
        observed_at_utc: wholeSecondUtc(now),
        source,
        reason,
        result: 'rejected-before-authority',
      });
      const projected = [...this.events, event];
      const projectedBytes = Buffer.byteLength(projected.map(item => canonicalJson(item)).join('\n') + '\n');
      if (projected.length > this.maximumEvents || projectedBytes > this.maximumBytes) {
        await this.#rotate();
        this.events = [];
      }
      this.events.push(event);
      await this.#writeCurrent();
      return event;
    });
    this.queue = operation.catch(() => {
      this.healthy = false;
    });
    return operation;
  }

  async #rotate() {
    const current = await regularFileOrMissing(this.path);
    if (!current || current.size === 0) return;
    if (await regularFileOrMissing(this.rotatedPath)) await unlink(this.rotatedPath);
    await rename(this.path, this.rotatedPath);
  }

  async #writeCurrent() {
    const bytes = Buffer.from(this.events.map(event => canonicalJson(event)).join('\n') + (this.events.length ? '\n' : ''), 'utf8');
    if (bytes.byteLength > this.maximumBytes) throw auditError('write-size-limit');
    const temporary = this.path + '.tmp-' + this.id();
    await writeFile(temporary, bytes, { flag: 'wx', mode: 0o600 });
    await rename(temporary, this.path);
  }
}

export function validateAuditReport(value) {
  if (!(value instanceof URLSearchParams)) throw auditError('report-query');
  if ([...value.keys()].sort().join(',') !== 'reason,schema_version,source') throw auditError('report-keys');
  if (value.getAll('reason').length !== 1 || value.getAll('schema_version').length !== 1 || value.getAll('source').length !== 1) {
    throw auditError('report-duplicates');
  }
  if (value.get('schema_version') !== 'vibapp.invalid-bootstrap-report.experimental-v1') throw auditError('report-schema');
  const reason = value.get('reason');
  const source = value.get('source');
  if (!REASON_SET.has(reason)) throw auditError('report-reason');
  if (!SOURCE_SET.has(source)) throw auditError('report-source');
  return { reason, source };
}

export function canonicalAuditJson(value) {
  return canonicalJson(value);
}
