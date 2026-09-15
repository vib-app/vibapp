import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import {
  CollaborationAdapter,
  MAX_MESSAGE_BYTES,
  MAX_SESSIONS,
  MAX_SESSION_TTL_MS,
  NIL_UUID
} from '../src/index.mjs'
import { createBrowserRoomHashTransport } from '../src/browser-roomhash-factory.mjs'

const IDS = Array.from({ length: 40 }, (_, index) => `00000000-0000-4000-8000-${String(index + 1).padStart(12, '0')}`)

function fixture({ allow = true, onMessage = () => true } = {}) {
  let now = 1_800_000_000_000
  let id = 0
  const joins = []
  const grants = []
  const transport = {
    async join(input) {
      const session = { input, sent: [], closed: false }
      joins.push(session)
      return {
        async send(bytes) { session.sent.push(bytes) },
        close() { session.closed = true },
        status() { return { connected: !session.closed } }
      }
    }
  }
  const adapter = new CollaborationAdapter({
    transport,
    authorize: async (grant, request) => {
      grants.push({ grant, request })
      return allow && grant === 'explicit-grant'
    },
    onMessage,
    clock: () => now,
    uuid: () => IDS[id++],
    schedule: () => ({ fixture: true }),
    cancel: () => {}
  })
  return { adapter, joins, grants, now: () => now, advance: (ms) => { now += ms } }
}

test('create and join require explicit grants and bounded expiry', async () => {
  const denied = fixture({ allow: false })
  await assert.rejects(denied.adapter.create({ grant: 'explicit-grant', expires_at: denied.now() + 1_000 }), /grant denied/)
  const fx = fixture()
  await assert.rejects(fx.adapter.create({ grant: null, expires_at: fx.now() + 1_000 }), /grant denied/)
  await assert.rejects(fx.adapter.create({ grant: 'explicit-grant', expires_at: fx.now() + MAX_SESSION_TTL_MS + 1 }), /expires_at/)
  const created = await fx.adapter.create({ grant: 'explicit-grant', expires_at: fx.now() + 1_000 })
  assert.match(created.channel_id, /^[0-9a-f-]{36}$/)
  assert.deepEqual(fx.grants.at(-1).request, { operation: 'create', channel_id: created.channel_id, expires_at: fx.now() + 1_000 })
})

test('trusted host can bind create approval to an exact preassigned channel', async () => {
  const fx = fixture()
  const channelId = IDS[30]
  const created = await fx.adapter.createAssigned({
    channel_id: channelId.toUpperCase(),
    grant: 'explicit-grant',
    expires_at: fx.now() + 1_000
  })
  assert.equal(created.channel_id, channelId)
  assert.deepEqual(fx.grants.at(-1).request, {
    operation: 'create',
    channel_id: channelId,
    expires_at: fx.now() + 1_000
  })
})

test('nil UUID is local-only and never reaches the network transport', async () => {
  const fx = fixture()
  await assert.rejects(fx.adapter.join({ channel_id: NIL_UUID, grant: 'explicit-grant', expires_at: fx.now() + 1_000 }), /non-nil/)
  assert.equal(fx.joins.length, 0)
})

test('exact request keys and RFC 4122 channel ids are enforced', async () => {
  const fx = fixture()
  await assert.rejects(fx.adapter.join({ channel_id: 'not-a-uuid', grant: 'explicit-grant', expires_at: fx.now() + 1_000 }), /RFC 4122/)
  await assert.rejects(fx.adapter.create({ grant: 'explicit-grant', expires_at: fx.now() + 1_000, gossip: true }), /unknown keys/)
  assert.equal(fx.joins.length, 0)
})

test('send copies bounded bytes and leave closes the transport session', async () => {
  const fx = fixture()
  const joined = await fx.adapter.join({ channel_id: IDS[30], grant: 'explicit-grant', expires_at: fx.now() + 10_000 })
  const bytes = new Uint8Array([1, 2, 3])
  assert.deepEqual(await fx.adapter.send({ session_id: joined.session_id, message: bytes }), { session_id: joined.session_id, accepted_bytes: 3 })
  bytes[0] = 9
  assert.deepEqual([...fx.joins[0].sent[0]], [1, 2, 3])
  await assert.rejects(fx.adapter.send({ session_id: joined.session_id, message: new Uint8Array(MAX_MESSAGE_BYTES + 1) }), /1../)
  assert.deepEqual(await fx.adapter.leave({ session_id: joined.session_id }), { session_id: joined.session_id, status: 'left' })
  assert.equal(fx.joins[0].closed, true)
})

test('expired sessions close and reject further messages', async () => {
  const fx = fixture()
  const joined = await fx.adapter.join({ channel_id: IDS[30], grant: 'explicit-grant', expires_at: fx.now() + 10 })
  fx.advance(11)
  assert.equal(fx.adapter.status().session_count, 0)
  assert.equal(fx.joins[0].closed, true)
  await assert.rejects(fx.adapter.send({ session_id: joined.session_id, message: new Uint8Array([1]) }), /expired/)
})

test('session count is hard limited', async () => {
  const fx = fixture()
  for (let index = 0; index < MAX_SESSIONS; index += 1) {
    await fx.adapter.join({ channel_id: IDS[index + 20], grant: 'explicit-grant', expires_at: fx.now() + 10_000 })
  }
  await assert.rejects(fx.adapter.join({ channel_id: IDS[39], grant: 'explicit-grant', expires_at: fx.now() + 10_000 }), /limit reached/)
  assert.equal(fx.joins.length, MAX_SESSIONS)
})

test('oversized inbound messages are dropped at the adapter boundary', async () => {
  const fx = fixture()
  await fx.adapter.join({ channel_id: IDS[30], grant: 'explicit-grant', expires_at: fx.now() + 10_000 })
  assert.equal(fx.joins[0].input.onMessage(new Uint8Array(MAX_MESSAGE_BYTES + 1)), false)
  assert.equal(fx.joins[0].input.onMessage(new Uint8Array([1])), true)
  assert.equal(fx.adapter.status().sessions[0].received_messages, 1)
})

test('accepted inbound messages are copied into the trusted host callback', async () => {
  const received = []
  const fx = fixture({ onMessage: (event) => { received.push(event); return true } })
  const joined = await fx.adapter.join({ channel_id: IDS[30], grant: 'explicit-grant', expires_at: fx.now() + 10_000 })
  const bytes = new Uint8Array([4, 5, 6])
  assert.equal(fx.joins[0].input.onMessage(bytes), true)
  bytes[0] = 9
  assert.equal(received[0].session_id, joined.session_id)
  assert.equal(received[0].channel_id, IDS[30])
  assert.deepEqual([...received[0].message], [4, 5, 6])
})

test('shared collaboration modules are browser-safe and exclude mesh gossip', async () => {
  const sources = await Promise.all([
    readFile(new URL('../src/index.mjs', import.meta.url), 'utf8'),
    readFile(new URL('../src/browser-roomhash-factory.mjs', import.meta.url), 'utf8')
  ])
  for (const source of sources) {
    assert.doesNotMatch(source, /(?:from\s*|import\s*\()['"]node:/)
    assert.doesNotMatch(source, /MeshNode|channel-list|crawlDiscovered|gossip/i)
  }
  assert.match(sources[0], /globalThis\.crypto\?\.randomUUID/)
})

test('browser factory exposes one bounded Trystero action and rejects nil UUID before join', async () => {
  const calls = []
  let action
  const transport = await createBrowserRoomHashTransport({
    trackerUrls: ['wss://tracker.example.test'],
    joinRoom(config, channelId) {
      calls.push({ config, channelId })
      action = { sent: [], async send(bytes) { this.sent.push(bytes) } }
      return {
        makeAction(name) {
          calls.push({ action: name })
          return action
        },
        leave() { calls.push({ leave: true }) }
      }
    }
  })
  await assert.rejects(
    transport.join({ channelId: NIL_UUID, onMessage: () => true }),
    /non-nil RFC 4122/,
  )
  assert.equal(calls.length, 0)
  const received = []
  const session = await transport.join({ channelId: IDS[30], onMessage: (bytes) => { received.push(bytes); return true } })
  assert.equal(calls[1].action, 'vibapp-collaboration-v1')
  assert.deepEqual(calls[0].config, {
    appId: 'roomhash-github-io-v2',
    relayConfig: { urls: ['wss://tracker.example.test/'] }
  })
  await session.send(new Uint8Array([1, 2]))
  assert.deepEqual([...action.sent[0]], [1, 2])
  assert.equal(action.onMessage(new Uint8Array([3])), true)
  assert.deepEqual([...received[0]], [3])
  session.close()
  await transport.close()
})

test('browser factory rejects plaintext tracker signaling', async () => {
  await assert.rejects(
    createBrowserRoomHashTransport({
      trackerUrls: ['ws://tracker.example.test'],
      joinRoom() { throw new Error('must not reach transport') }
    }),
    /tracker URL must use wss/
  )
})

test('default UUID source uses the cross-runtime Web Crypto API', async () => {
  const adapter = new CollaborationAdapter({
    transport: {
      async join() {
        return { async send() {}, close() {} }
      }
    },
    authorize: async () => true
  })
  const created = await adapter.create({ grant: 'explicit-grant', expires_at: Date.now() + 1_000 })
  assert.match(created.channel_id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i)
  await adapter.leave({ session_id: created.session_id })
  await adapter.close()
})
