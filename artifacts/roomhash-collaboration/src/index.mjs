export const NIL_UUID = '00000000-0000-0000-0000-000000000000'
export const MAX_SESSIONS = 16
export const MAX_MESSAGE_BYTES = 64 * 1024
export const MAX_SESSION_TTL_MS = 24 * 60 * 60 * 1000

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i

function exactObject(value, keys, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new TypeError(`${label} must be an object`)
  const actual = Object.keys(value).sort()
  const expected = [...keys].sort()
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    throw new TypeError(`${label} contains missing or unknown keys`)
  }
  return value
}

function validUuid(value) {
  return typeof value === 'string' && UUID_RE.test(value) && value.toLowerCase() !== NIL_UUID
}

function browserUuid() {
  const randomUUID = globalThis.crypto?.randomUUID
  if (typeof randomUUID !== 'function') throw new Error('crypto.randomUUID is unavailable; inject a UUID source')
  return randomUUID.call(globalThis.crypto)
}

function normalizeExpiry(value, now) {
  if (!Number.isSafeInteger(value) || value <= now || value > now + MAX_SESSION_TTL_MS) {
    throw new RangeError(`expires_at must be within the next ${MAX_SESSION_TTL_MS} milliseconds`)
  }
  return value
}

function closeQuietly(session) {
  try {
    return Promise.resolve(session.transport.close()).catch(() => {})
  } catch {
    return Promise.resolve()
  }
}

export class CollaborationAdapter {
  #transport
  #authorize
  #clock
  #uuid
  #onMessage
  #schedule
  #cancel
  #sessions = new Map()
  #closed = false

  constructor({ transport, authorize, onMessage = () => true, clock = () => Date.now(), uuid = browserUuid, schedule = setTimeout, cancel = clearTimeout }) {
    if (!transport || typeof transport.join !== 'function') throw new TypeError('transport.join is required')
    if (typeof authorize !== 'function') throw new TypeError('an explicit grant authorizer is required')
    if (typeof onMessage !== 'function') throw new TypeError('onMessage must be a function')
    this.#transport = transport
    this.#authorize = authorize
    this.#onMessage = onMessage
    this.#clock = clock
    this.#uuid = uuid
    this.#schedule = schedule
    this.#cancel = cancel
  }

  async create(input) {
    exactObject(input, ['grant', 'expires_at'], 'create request')
    const channelId = this.#uuid()
    if (!validUuid(channelId)) throw new Error('UUID source returned an invalid or local-only channel id')
    return this.#open('create', channelId, input.grant, input.expires_at)
  }

  // Trusted launcher hosts use this entry point after they have generated a
  // channel id and bound an explicit approval grant to that exact id. Keeping
  // it separate preserves the convenient browser create API while preventing
  // a process-boundary grant from authorizing an unknown future channel.
  async createAssigned(input) {
    exactObject(input, ['channel_id', 'grant', 'expires_at'], 'assigned create request')
    if (!validUuid(input.channel_id)) throw new Error('network channel_id must be a non-nil RFC 4122 UUID')
    return this.#open('create', input.channel_id.toLowerCase(), input.grant, input.expires_at)
  }

  async join(input) {
    exactObject(input, ['channel_id', 'grant', 'expires_at'], 'join request')
    if (!validUuid(input.channel_id)) throw new Error('network channel_id must be a non-nil RFC 4122 UUID')
    return this.#open('join', input.channel_id.toLowerCase(), input.grant, input.expires_at)
  }

  async leave(input) {
    exactObject(input, ['session_id'], 'leave request')
    this.#expire()
    const session = this.#sessions.get(input.session_id)
    if (!session) throw new Error('unknown collaboration session')
    this.#sessions.delete(input.session_id)
    this.#cancel(session.timer)
    await closeQuietly(session)
    return { session_id: input.session_id, status: 'left' }
  }

  async send(input) {
    exactObject(input, ['session_id', 'message'], 'send request')
    this.#expire()
    const session = this.#sessions.get(input.session_id)
    if (!session) throw new Error('unknown or expired collaboration session')
    if (!(input.message instanceof Uint8Array)) throw new TypeError('message must be Uint8Array bytes')
    if (input.message.byteLength === 0 || input.message.byteLength > MAX_MESSAGE_BYTES) {
      throw new RangeError(`message must contain 1..${MAX_MESSAGE_BYTES} bytes`)
    }
    await session.transport.send(input.message.slice())
    session.sentMessages += 1
    session.sentBytes += input.message.byteLength
    return { session_id: input.session_id, accepted_bytes: input.message.byteLength }
  }

  status() {
    this.#expire()
    return {
      status: this.#closed ? 'closed' : 'ready',
      session_count: this.#sessions.size,
      limits: {
        max_sessions: MAX_SESSIONS,
        max_message_bytes: MAX_MESSAGE_BYTES,
        max_session_ttl_ms: MAX_SESSION_TTL_MS
      },
      sessions: [...this.#sessions.values()].map((session) => ({
        session_id: session.sessionId,
        channel_id: session.channelId,
        expires_at: session.expiresAt,
        received_messages: session.receivedMessages,
        received_bytes: session.receivedBytes,
        sent_messages: session.sentMessages,
        sent_bytes: session.sentBytes,
        transport: typeof session.transport.status === 'function' ? session.transport.status() : null
      }))
    }
  }

  async close() {
    if (this.#closed) return
    this.#closed = true
    const sessions = [...this.#sessions.values()]
    this.#sessions.clear()
    for (const session of sessions) this.#cancel(session.timer)
    await Promise.all(sessions.map(closeQuietly))
    if (typeof this.#transport.close === 'function') await this.#transport.close()
  }

  async #open(operation, channelId, grant, expiresAtValue) {
    if (this.#closed) throw new Error('collaboration adapter is closed')
    this.#expire()
    if (this.#sessions.size >= MAX_SESSIONS) throw new Error(`collaboration session limit reached (${MAX_SESSIONS})`)
    const now = this.#clock()
    const expiresAt = normalizeExpiry(expiresAtValue, now)
    const authorized = await this.#authorize(grant, {
      operation,
      channel_id: channelId,
      expires_at: expiresAt
    })
    if (authorized !== true) throw new Error('explicit collaboration grant denied')
    const sessionId = this.#uuid()
    if (!validUuid(sessionId) || this.#sessions.has(sessionId)) throw new Error('UUID source returned an invalid session id')
    const counters = { receivedMessages: 0, receivedBytes: 0 }
    const transport = await this.#transport.join({
      channelId,
      onMessage: (message) => {
        if (!(message instanceof Uint8Array) || message.byteLength === 0 || message.byteLength > MAX_MESSAGE_BYTES) return false
        let accepted = false
        try {
          accepted = this.#onMessage({
            session_id: sessionId,
            channel_id: channelId,
            message: message.slice()
          }) === true
        } catch {
          return false
        }
        if (!accepted) return false
        counters.receivedMessages += 1
        counters.receivedBytes += message.byteLength
        return true
      }
    })
    if (!transport || typeof transport.send !== 'function' || typeof transport.close !== 'function') {
      throw new TypeError('transport.join returned an invalid session')
    }
    const session = {
      sessionId,
      channelId,
      expiresAt,
      transport,
      timer: null,
      sentMessages: 0,
      sentBytes: 0,
      get receivedMessages() { return counters.receivedMessages },
      get receivedBytes() { return counters.receivedBytes }
    }
    const delay = Math.max(1, expiresAt - this.#clock())
    session.timer = this.#schedule(() => {
      if (this.#sessions.delete(sessionId)) void closeQuietly(session)
    }, delay)
    this.#sessions.set(sessionId, session)
    return { session_id: sessionId, channel_id: channelId, expires_at: expiresAt }
  }

  #expire() {
    const now = this.#clock()
    for (const [sessionId, session] of this.#sessions) {
      if (session.expiresAt <= now) {
        this.#sessions.delete(sessionId)
        this.#cancel(session.timer)
        void closeQuietly(session)
      }
    }
  }
}
