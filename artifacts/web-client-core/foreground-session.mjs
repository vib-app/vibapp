// Foreground-only worker protocol, separate from the one-shot bootstrap contract.
const SCHEMA = 'vibapp.foreground-session.experimental-v1';
const OPERATIONS = new Set(['refresh', 'action', 'close']);
const BINDING_KEYS = ['entrypoint', 'package_digest_sha256', 'component_sha256', 'generation', 'session', 'surface', 'route'];
const MAX_MESSAGE_BYTES = 128 * 1024;
const equalBinding = (actual, expected) => actual && Object.keys(actual).sort().join(',') === BINDING_KEYS.slice().sort().join(',') && BINDING_KEYS.every(key => actual[key] === expected[key]);

export function foregroundBinding(request, surface) {
  return {
    entrypoint: 'main', package_digest_sha256: request.package_digest_sha256,
    component_sha256: request.component_sha256,
    generation: 'web-generation-' + request.component_sha256.slice(0, 24),
    session: request.session, surface: surface.surface, route: surface.route,
  };
}

function bounded(value) {
  if (new TextEncoder().encode(JSON.stringify(value)).byteLength > MAX_MESSAGE_BYTES) throw new Error('foreground-message-limit');
}

export function foregroundFields(definitions, changes) {
  if (!Array.isArray(changes) || changes.length > 64) throw new Error('foreground-field-limit');
  bounded(changes);
  const seen = new Set();
  return changes.map(change => {
    const field = definitions.find(item => item.field === change?.field);
    const value = change?.value;
    if (!field || field.sensitive || seen.has(field.field) || !value || !['empty', field.kind].includes(value.tag)) throw new Error('foreground-field-invalid');
    seen.add(field.field);
    // Like the native host, the transport validates shape, not form submission.
    // A required add-item editor must not block an unrelated Delete/Cancel action.
    if (value.tag === 'empty') return { field: field.field, value: { tag: 'empty' } };
    if (value.tag === 'integer' && !Number.isSafeInteger(value.value)) throw new Error('foreground-integer-invalid');
    if (value.tag === 'boolean' && typeof value.value !== 'boolean') throw new Error('foreground-boolean-invalid');
    if (['text', 'decimal', 'time-zone', 'choice'].includes(value.tag) && (typeof value.value !== 'string' || value.value.length > 16384)) throw new Error('foreground-text-limit');
    if (value.tag === 'decimal' && !/^-?(?:\d+\.?\d*|\.\d+)$/.test(value.value)) throw new Error('foreground-decimal-invalid');
    if (value.tag === 'choice' && !field.choices.some(item => item.value === value.value)) throw new Error('foreground-choice-invalid');
    if (value.tag === 'date') {
      const date = value.value;
      if (!date || !['year', 'month', 'day'].every(key => Number.isInteger(date[key])) || date.year < 1 || date.year > 9999) throw new Error('foreground-date-invalid');
      const observed = new Date(0); observed.setUTCFullYear(date.year, date.month - 1, date.day);
      if (observed.getUTCFullYear() !== date.year || observed.getUTCMonth() + 1 !== date.month || observed.getUTCDate() !== date.day) throw new Error('foreground-date-invalid');
    }
    if (value.tag === 'time') {
      const time = value.value;
      if (!time || !['hour', 'minute', 'second'].every(key => Number.isInteger(time[key]) && time[key] >= 0 && time[key] < (key === 'hour' ? 24 : 60))) throw new Error('foreground-time-invalid');
    }
    return { field: field.field, value: { tag: value.tag, val: value.tag === 'integer' ? BigInt(value.value) : value.value } };
  });
}

export function bindForegroundWorker(port, binding, onEvent, close) {
  let sequence = 0;
  let closed = false;
  port.onmessage = event => {
    if (closed) return;
    try {
      const message = event.data;
      bounded(message);
      if (!message || Object.keys(message).sort().join(',') !== 'action,binding,event_id,fields,operation,schema_version,sequence'
        || message.schema_version !== SCHEMA || !OPERATIONS.has(message.operation)
        || !Number.isSafeInteger(message.sequence) || message.sequence !== sequence + 1 || message.sequence > 100000
        || typeof message.event_id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(message.event_id)
        || !equalBinding(message.binding, binding)) throw new Error('foreground-stale-or-forged-message');
      sequence = message.sequence;
      if (message.operation === 'close') {
        closed = true; port.close(); close(); return;
      }
      let result = null; let error = null;
      try { result = onEvent(message); bounded(result); }
      catch (failure) { error = failure instanceof Error ? failure.message.slice(0, 1000) : 'foreground-event-failed'; }
      port.postMessage({ schema_version: SCHEMA, sequence, event_id: message.event_id, binding, result, error });
    } catch {
      // Invalid/replayed traffic cannot keep a worker alive or mutate guest state.
      closed = true; port.close(); close();
    }
  };
}

export class ForegroundClient {
  constructor({ port, worker, binding, appId, timeoutMs = 2000 }) {
    if (typeof appId !== 'string' || !appId || !equalBinding(binding, binding)) throw new Error('foreground-invalid-identity');
    this.port = port; this.worker = worker; this.binding = binding;
    this.appId = appId;
    this.sequence = 0; this.pending = null; this.closed = false; this.timeoutMs = timeoutMs;
    port.onmessage = event => {
      const pending = this.pending;
      if (!pending || this.closed) { this.close(new Error('foreground-unexpected-response')); return; }
      try {
        const response = event.data;
        bounded(response);
        if (!response || Object.keys(response).sort().join(',') !== 'binding,error,event_id,result,schema_version,sequence'
          || response.schema_version !== SCHEMA || response.sequence !== this.sequence
          || response.event_id !== pending.eventId || !equalBinding(response.binding, this.binding)
          || (response.error !== null && typeof response.error !== 'string')) throw new Error('foreground-response-binding-mismatch');
        this.pending = null; clearTimeout(pending.timeout);
        if (response.error !== null) pending.reject(new Error(response.error));
        else pending.resolve(response.result);
      } catch (error) { this.close(error); }
    };
    worker.onerror = () => this.close(new Error('foreground-worker-failed'));
  }

  call(operation, payload) {
    if (this.closed || this.pending || !['refresh', 'action'].includes(operation)) return Promise.reject(new Error('foreground-session-unavailable'));
    const expected = this.binding;
    if (!payload || payload.appId !== this.appId || !BINDING_KEYS.every(key => {
      const wireKey = ({ package_digest_sha256: 'packageDigestSha256', component_sha256: 'componentSha256' })[key] || key;
      return payload[wireKey] === expected[key];
    })) return Promise.reject(new Error('foreground-stale-binding'));
    const message = { schema_version: SCHEMA, operation, sequence: this.sequence + 1,
      event_id: payload.eventId, binding: expected, action: payload.action ?? null, fields: payload.fields ?? [] };
    try { bounded(message); } catch (error) { return Promise.reject(error); }
    this.sequence++;
    return new Promise((resolve, reject) => {
      this.pending = { resolve, reject, eventId: payload.eventId,
        timeout: setTimeout(() => this.close(new Error('foreground-worker-timeout')), this.timeoutMs) };
      try { this.port.postMessage(message); } catch (error) { this.close(error); }
    });
  }

  close(error = new Error('foreground-session-closed')) {
    if (this.closed) return;
    this.closed = true;
    const pending = this.pending; this.pending = null;
    if (pending) { clearTimeout(pending.timeout); pending.reject(error); }
    this.port.close(); this.worker.terminate();
  }
}
