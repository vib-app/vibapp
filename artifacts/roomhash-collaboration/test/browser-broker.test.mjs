import assert from 'node:assert/strict'
import test from 'node:test'
import { createBrowserCollaborationBroker } from '../src/browser-broker.mjs'
import { MAX_MESSAGE_BYTES, NIL_UUID } from '../src/index.mjs'

const IDS = Array.from({ length: 20 }, (_, index) => `10000000-0000-4000-8000-${String(index + 1).padStart(12, '0')}`)

function inbound(fx, session, index) {
  return new TextEncoder().encode(JSON.stringify({
    schema_version: 'vibapp.collaboration-event.experimental-v1',
    app_id: 'demo.shared-app', channel_id: session.channelId,
    event_id: `20000000-0000-4000-8000-${String(index).padStart(12, '0')}`,
    sent_at: fx.now(), payload: { move: index }
  }))
}

test('expiry frees queue capacity and late transport callbacks cannot resurrect a session', async () => {
  const fx = fixture()
  const broker = await fx.create()
  const first = await broker.create({ confirmed: true, expiresInMs: 60_000 })
  for (let i = 0; i < 128; i++) assert.equal(fx.joins[0].input.onMessage(inbound(fx, first, i)), true)
  assert.equal(broker.status().queuedEvents, 128)
  fx.advance(60_001)
  assert.throws(() => broker.receive({ sessionId: first.sessionId, limit: 1 }), /unknown/)
  assert.equal(broker.status().queuedBytes, 0)
  assert.equal(broker.status().deduplicationEntries, 0)
  assert.equal(fx.joins[0].input.onMessage(inbound(fx, first, 129)), false)
  await broker.leave({ sessionId: first.sessionId })
  const second = await broker.create({ confirmed: true, expiresInMs: 60_000 })
  assert.equal(fx.joins[1].input.onMessage(inbound(fx, second, 0)), true)
  assert.equal(broker.status().queuedEvents, 1)
  await broker.close()
})

test('deduplication is session-scoped and bounded after messages are consumed', async () => {
  const fx = fixture()
  const broker = await fx.create()
  const first = await broker.create({ confirmed: true, expiresInMs: 60_000 })
  const second = await broker.create({ confirmed: true, expiresInMs: 60_000 })
  for (let i = 0; i < 1100; i++) {
    const bytes = inbound(fx, first, i)
    assert.equal(fx.joins[0].input.onMessage(bytes), true)
    assert.equal(fx.joins[0].input.onMessage(bytes), false)
    broker.receive({ sessionId: first.sessionId, limit: 1 })
  }
  assert.equal(broker.status().deduplicationEntries, 1024)
  assert.equal(fx.joins[1].input.onMessage(inbound(fx, second, 1099)), true)
  await broker.leave({ sessionId: first.sessionId })
  assert.equal(broker.status().deduplicationEntries, 1)
  await broker.close()
})

function fixture() {
  let now = 1_800_000_000_000
  let uuidIndex = 0
  const joins = []
  const transport = {
    async join(input) {
      const joined = { input, sent: [], closed: false }
      joins.push(joined)
      return {
        async send(bytes) { joined.sent.push(bytes.slice()) },
        close() { joined.closed = true },
        status() { return { status: joined.closed ? 'closed' : 'joined', peer_count: 2 } }
      }
    },
    async close() {}
  }
  return {
    now: () => now,
    advance: value => { now += value },
    joins,
    create: options => createBrowserCollaborationBroker({
      appId: 'demo.shared-app',
      settings: { rtcEnabled: true, turnEnabled: false, maxActiveChannels: 2 },
      transport,
      clock: () => now,
      uuid: () => IDS[uuidIndex++],
      ...options
    })
  }
}

test('trusted browser broker requires explicit create/join confirmation and rejects local-only UUID', async () => {
  const fx = fixture()
  const broker = await fx.create()
  await assert.rejects(broker.create({ confirmed: false, expiresInMs: 60_000 }), /confirmation/)
  await assert.rejects(broker.join({ channelId: NIL_UUID, confirmed: true, expiresInMs: 60_000 }), /non-nil/)
  assert.equal(fx.joins.length, 0)
  const created = await broker.create({ confirmed: true, expiresInMs: 60_000 })
  assert.equal(created.channelId, IDS[0])
  assert.equal(created.sessionId, IDS[2])
  assert.equal(fx.joins.length, 1)
  await broker.close()
})

test('broker wraps typed app-bound events and returns only validated inbound messages', async () => {
  const fx = fixture()
  const broker = await fx.create()
  const joined = await broker.join({ channelId: IDS[10], confirmed: true, expiresInMs: 60_000 })
  const sent = await broker.send({ sessionId: joined.sessionId, eventId: IDS[11], payload: { move: 3 } })
  assert.equal(sent.eventId, IDS[11])
  const outbound = JSON.parse(new TextDecoder().decode(fx.joins[0].sent[0]))
  assert.deepEqual(outbound, {
    schema_version: 'vibapp.collaboration-event.experimental-v1',
    app_id: 'demo.shared-app',
    channel_id: IDS[10],
    event_id: IDS[11],
    sent_at: fx.now(),
    payload: { move: 3 }
  })
  const inbound = new TextEncoder().encode(JSON.stringify({
    ...outbound,
    event_id: IDS[12],
    payload: { move: 4 }
  }))
  assert.equal(fx.joins[0].input.onMessage(inbound), true)
  inbound[0] ^= 0xff
  assert.equal(fx.joins[0].input.onMessage(new TextEncoder().encode(JSON.stringify({ ...outbound, app_id: 'other.app' }))), false)
  assert.deepEqual(broker.receive({ sessionId: joined.sessionId, limit: 4 }), [{
    channelId: IDS[10],
    event: { ...outbound, event_id: IDS[12], payload: { move: 4 } }
  }])
  assert.equal(broker.status().sessions[0].peerCount, 2)
  assert.deepEqual(broker.receive({ sessionId: joined.sessionId, limit: 4 }), [])
  await broker.leave({ sessionId: joined.sessionId })
  assert.equal(fx.joins[0].closed, true)
  await broker.close()
})

test('broker enforces settings, session, expiry, and encoded message limits', async () => {
  const fx = fixture()
  await assert.rejects(fx.create({ settings: { rtcEnabled: false, turnEnabled: false, maxActiveChannels: 2 } }), /RTC is disabled/)
  await assert.rejects(fx.create({ settings: { rtcEnabled: true, turnEnabled: true, maxActiveChannels: 2 } }), /write-only/)
  const broker = await fx.create()
  const first = await broker.create({ confirmed: true, expiresInMs: 60_000 })
  await assert.rejects(broker.create({ confirmed: true, expiresInMs: 59_999 }), /expires_in_ms/)
  await broker.create({ confirmed: true, expiresInMs: 60_000 })
  await assert.rejects(broker.create({ confirmed: true, expiresInMs: 60_000 }), /channel limit/)
  await assert.rejects(broker.send({
    sessionId: first.sessionId,
    eventId: IDS[15],
    payload: 'x'.repeat(MAX_MESSAGE_BYTES)
  }), /exceeds/)
  await broker.close()
})
