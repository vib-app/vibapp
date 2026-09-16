// Trusted foreground-only host. No DOM, network, ambient WASI or durable storage.
// This first product policy is intentionally read-only: stateless tools such as
// clocks may run; apps requiring saved data/settings need another host policy.
const denied = () => { throw { code: 'capability-unavailable', message: 'This browser profile has no persistent state.', retryable: false }; };
export function wallNow() {
  const now = new Date();
  return { nowUtc: now.toISOString().replace(/\.\d{3}Z$/, 'Z'),
    timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
    utcOffsetSeconds: -now.getTimezoneOffset() * 60 };
}
export function monotonicNow() { return BigInt(Math.floor(performance.now())); }
export function describeHost() {
  return { profile: 'web-runtime', background: 'foreground-only', capabilities:
    ['clock', 'host-info', 'kv', 'log', 'settings'].map(name => ({
      interfaceName: `vibapp:experimental-v0/${name}@0.0.1`, availability: 'brokered', granted: true,
      detail: name === 'kv' || name === 'settings' ? 'Empty read-only host state; no persistence.' : 'Bounded foreground host.',
    })) };
}
export function get() { return undefined; }
export function scanPrefix(prefix, limit) {
  if (typeof prefix !== 'string' || prefix.length > 128 || !Number.isInteger(limit) || limit < 0 || limit > 1024) denied();
  return [];
}
export function transact() { denied(); }
export function current() { return { schemaRevision: 1n, configRevision: 1n, values: [] }; }
export function write(level, message, fields, key) {
  if (!['debug', 'info', 'warn', 'error'].includes(level) || typeof message !== 'string' || message.length > 4096
    || !Array.isArray(fields) || fields.length > 32 || typeof key !== 'string' || key.length > 128) denied();
  // Discarding diagnostics is explicit; there is no console/credential sink.
}
