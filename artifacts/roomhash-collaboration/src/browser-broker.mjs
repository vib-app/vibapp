import { CollaborationAdapter, MAX_MESSAGE_BYTES, MAX_SESSION_TTL_MS, NIL_UUID } from './index.mjs'
import { createBrowserRoomHashTransport } from './browser-roomhash-factory.mjs'

const MAX_BROKER_SESSIONS = 16
const MAX_QUEUED_EVENTS = 128
const MAX_QUEUED_BYTES = 1024 * 1024
// Duplicate suppression is scoped to a session's most recent events, not an
// ever-growing, cross-channel ledger. Applications still own domain idempotency.
const MAX_SEEN_EVENTS_PER_SESSION = 1024
const MIN_SESSION_TTL_MS = 60 * 1000
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const APP_ID_RE = /^[a-z0-9][a-z0-9._-]{0,127}$/

function exactObject(value, keys, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new TypeError(`${label} must be an object`)
  const actual = Object.keys(value).sort()
  const expected = [...keys].sort()
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    throw new TypeError(`${label} contains missing or unknown keys`)
  }
  return value
}

function uuidValue(value, label) {
  if (typeof value !== 'string' || !UUID_RE.test(value) || value.toLowerCase() === NIL_UUID) {
    throw new TypeError(`${label} must be a non-nil RFC 4122 UUID`)
  }
  return value.toLowerCase()
}

function freshUuid(uuid, label) {
  return uuidValue(uuid(), label)
}

function ttlExpiry(value, now) {
  if (!Number.isSafeInteger(value) || value < MIN_SESSION_TTL_MS || value > MAX_SESSION_TTL_MS) {
    throw new RangeError(`expires_in_ms must be ${MIN_SESSION_TTL_MS}..${MAX_SESSION_TTL_MS}`)
  }
  return now + value
}

function encodedEvent(value) {
  let text
  try {
    text = JSON.stringify(value)
  } catch {
    throw new TypeError('collaboration event must be JSON serializable')
  }
  if (typeof text !== 'string') throw new TypeError('collaboration event must be JSON serializable')
  const bytes = new TextEncoder().encode(text)
  if (bytes.byteLength < 1 || bytes.byteLength > MAX_MESSAGE_BYTES) {
    throw new RangeError(`collaboration event exceeds ${MAX_MESSAGE_BYTES} bytes`)
  }
  return bytes
}

function decodedEvent(bytes, expectedAppId, expectedChannelId) {
  let value
  try {
    value = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes))
  } catch {
    return null
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  if (Object.keys(value).sort().join(',') !== 'app_id,channel_id,event_id,payload,schema_version,sent_at') return null
  if (
    value.schema_version !== 'vibapp.collaboration-event.experimental-v1'
    || value.app_id !== expectedAppId
    || value.channel_id !== expectedChannelId
    || !UUID_RE.test(value.event_id || '')
    || !Number.isSafeInteger(value.sent_at)
    || value.sent_at < 0
  ) return null
  return value
}

function normalizedSettings(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new TypeError('network settings must be an object')
  if (value.rtcEnabled !== true) throw new Error('RTC is disabled in network settings')
  if (value.turnEnabled === true) {
    throw new Error('custom TURN credentials are write-only and unavailable to the website foreground peer')
  }
  if (!Number.isSafeInteger(value.maxActiveChannels) || value.maxActiveChannels < 1 || value.maxActiveChannels > 32) {
    throw new RangeError('maxActiveChannels must be 1..32')
  }
  return Object.freeze({ maxActiveChannels: Math.min(MAX_BROKER_SESSIONS, value.maxActiveChannels) })
}

export async function createBrowserCollaborationBroker({
  appId,
  settings,
  transport,
  clock = () => Date.now(),
  uuid = () => globalThis.crypto.randomUUID()
}) {
  if (typeof appId !== 'string' || !APP_ID_RE.test(appId)) throw new TypeError('appId is invalid')
  const policy = normalizedSettings(settings)
  const roomHashTransport = transport ?? await createBrowserRoomHashTransport()
  if (!roomHashTransport || typeof roomHashTransport.join !== 'function') throw new TypeError('RoomHash transport is invalid')
  const approvals = new Map()
  const sessions = new Map()
  const queued = []
  const seenEvents = new Map()
  let queuedBytes = 0
  let closed = false

  const adapter = new CollaborationAdapter({
    transport: roomHashTransport,
    clock,
    uuid,
    authorize(grant, request) {
      const approved = approvals.get(grant)
      approvals.delete(grant)
      return Boolean(
        approved
        && approved.operation === request.operation
        && approved.channelId === request.channel_id
        && approved.expiresAt === request.expires_at
        && approved.expiresAt > clock()
      )
    },
    onMessage(input) {
      pruneExpired()
      if (closed || !sessions.has(input.session_id)) return false
      const value = decodedEvent(input.message, appId, input.channel_id)
      const seen = seenEvents.get(input.session_id) ?? new Set()
      if (!value || seen.has(value.event_id.toLowerCase())) return false
      if (queued.length >= MAX_QUEUED_EVENTS || queuedBytes + input.message.byteLength > MAX_QUEUED_BYTES) return false
      seen.add(value.event_id.toLowerCase())
      if (seen.size > MAX_SEEN_EVENTS_PER_SESSION) seen.delete(seen.values().next().value)
      seenEvents.set(input.session_id, seen)
      queuedBytes += input.message.byteLength
      queued.push(Object.freeze({
        sessionId: input.session_id,
        channelId: input.channel_id,
        byteLength: input.message.byteLength,
        event: value
      }))
      return true
    }
  })

  function forgetSession(sessionId) {
    sessions.delete(sessionId)
    seenEvents.delete(sessionId)
    for (let index = queued.length - 1; index >= 0; index -= 1) {
      if (queued[index].sessionId === sessionId) {
        queuedBytes -= queued[index].byteLength
        queued.splice(index, 1)
      }
    }
  }

  function pruneExpired() {
    const raw = adapter.status()
    const active = new Set(raw.sessions.map(session => session.session_id))
    for (const sessionId of sessions.keys()) {
      if (!active.has(sessionId)) forgetSession(sessionId)
    }
    return raw
  }

  function approve(operation, channelId, expiresAt) {
    const grant = freshUuid(uuid, 'approval grant')
    approvals.set(grant, { operation, channelId, expiresAt })
    return grant
  }

  function ensureOpen() {
    if (closed) throw new Error('collaboration broker is closed')
    pruneExpired()
    if (sessions.size >= policy.maxActiveChannels) throw new Error('configured collaboration channel limit reached')
  }

  async function create(input) {
    exactObject(input, ['confirmed', 'expiresInMs'], 'create collaboration request')
    if (input.confirmed !== true) throw new Error('explicit user confirmation is required')
    ensureOpen()
    const channelId = freshUuid(uuid, 'channel id')
    const expiresAt = ttlExpiry(input.expiresInMs, clock())
    const grant = approve('create', channelId, expiresAt)
    try {
      const result = await adapter.createAssigned({ channel_id: channelId, grant, expires_at: expiresAt })
      sessions.set(result.session_id, { channelId: result.channel_id, expiresAt: result.expires_at })
      return Object.freeze({ sessionId: result.session_id, channelId: result.channel_id, expiresAt: result.expires_at })
    } finally {
      approvals.delete(grant)
    }
  }

  async function join(input) {
    exactObject(input, ['channelId', 'confirmed', 'expiresInMs'], 'join collaboration request')
    if (input.confirmed !== true) throw new Error('explicit user confirmation is required')
    ensureOpen()
    const channelId = uuidValue(input.channelId, 'channelId')
    const expiresAt = ttlExpiry(input.expiresInMs, clock())
    const grant = approve('join', channelId, expiresAt)
    try {
      const result = await adapter.join({ channel_id: channelId, grant, expires_at: expiresAt })
      sessions.set(result.session_id, { channelId: result.channel_id, expiresAt: result.expires_at })
      return Object.freeze({ sessionId: result.session_id, channelId: result.channel_id, expiresAt: result.expires_at })
    } finally {
      approvals.delete(grant)
    }
  }

  async function send(input) {
    exactObject(input, ['eventId', 'payload', 'sessionId'], 'send collaboration request')
    pruneExpired()
    const session = sessions.get(input.sessionId)
    if (!session) throw new Error('unknown collaboration session')
    const eventId = uuidValue(input.eventId, 'eventId')
    const message = encodedEvent({
      schema_version: 'vibapp.collaboration-event.experimental-v1',
      app_id: appId,
      channel_id: session.channelId,
      event_id: eventId,
      sent_at: clock(),
      payload: input.payload
    })
    const result = await adapter.send({ session_id: input.sessionId, message })
    return Object.freeze({ sessionId: input.sessionId, eventId, acceptedBytes: result.accepted_bytes })
  }

  async function leave(input) {
    exactObject(input, ['sessionId'], 'leave collaboration request')
    uuidValue(input.sessionId, 'sessionId')
    pruneExpired()
    if (!sessions.has(input.sessionId)) return Object.freeze({ sessionId: input.sessionId, status: 'left' })
    try {
      const result = await adapter.leave({ session_id: input.sessionId })
      return Object.freeze({ sessionId: result.session_id, status: result.status })
    } finally {
      forgetSession(input.sessionId)
    }
  }

  function receive(input) {
    exactObject(input, ['limit', 'sessionId'], 'receive collaboration request')
    pruneExpired()
    if (!sessions.has(input.sessionId)) throw new Error('unknown collaboration session')
    if (!Number.isSafeInteger(input.limit) || input.limit < 1 || input.limit > 64) throw new RangeError('receive limit must be 1..64')
    const values = []
    for (let index = 0; index < queued.length && values.length < input.limit;) {
      if (queued[index].sessionId !== input.sessionId) {
        index += 1
        continue
      }
      const [item] = queued.splice(index, 1)
      queuedBytes -= item.byteLength
      values.push(Object.freeze({ channelId: item.channelId, event: item.event }))
    }
    return Object.freeze(values)
  }

  function status() {
    const raw = pruneExpired()
    return Object.freeze({
      state: closed ? 'closed' : 'ready',
      appId,
      sessionCount: sessions.size,
      queuedEvents: queued.length,
      queuedBytes,
      deduplicationEntries: [...seenEvents.values()].reduce((total, seen) => total + seen.size, 0),
      maxActiveChannels: policy.maxActiveChannels,
      sessions: Object.freeze(raw.sessions.map(session => Object.freeze({
        sessionId: session.session_id,
        channelId: session.channel_id,
        expiresAt: session.expires_at,
        peerCount: Number.isSafeInteger(session.transport?.peer_count) ? session.transport.peer_count : 0,
        sentMessages: session.sent_messages,
        receivedMessages: session.received_messages
      })))
    })
  }

  async function close() {
    if (closed) return
    closed = true
    approvals.clear()
    sessions.clear()
    queued.length = 0
    queuedBytes = 0
    seenEvents.clear()
    await adapter.close()
  }

  return Object.freeze({ create, join, send, leave, receive, status, close })
}
