import { MAX_MESSAGE_BYTES, NIL_UUID } from './index.mjs'

const APP_ID = 'roomhash-github-io-v2'
const ACTION = 'vibapp-collaboration-v1'
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const DEFAULT_TRACKERS = Object.freeze([
  'wss://tracker.webtorrent.dev',
  'wss://tracker.openwebtorrent.com',
  'wss://tracker.btorrent.xyz'
])

function exactObject(value, allowed, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new TypeError(`${label} must be an object`)
  const extras = Object.keys(value).filter((key) => !allowed.includes(key))
  if (extras.length) throw new TypeError(`${label} contains unknown keys`)
  return value
}

function validChannelId(value) {
  return typeof value === 'string' && UUID_RE.test(value) && value.toLowerCase() !== NIL_UUID
}

function trackerUrls(values) {
  if (!Array.isArray(values) || values.length < 1 || values.length > 8) throw new TypeError('trackerUrls must contain 1..8 URLs')
  return Object.freeze(values.map((value) => {
    if (typeof value !== 'string' || value.length > 2048) throw new TypeError('tracker URL is invalid')
    const url = new URL(value)
    if (url.protocol !== 'wss:') throw new TypeError('tracker URL must use wss')
    return url.href
  }))
}

function turnConfig(value) {
  if (value == null) return []
  exactObject(value, ['urls', 'username', 'credential'], 'turn')
  if (!Array.isArray(value.urls) || value.urls.length < 1 || value.urls.length > 8) throw new TypeError('turn.urls must contain 1..8 URLs')
  const urls = value.urls.map((item) => {
    if (typeof item !== 'string' || item.length > 2048 || !/^turns?:/i.test(item)) throw new TypeError('TURN URL is invalid')
    return item
  })
  if (typeof value.username !== 'string' || value.username.length > 256) throw new TypeError('TURN username is invalid')
  if (typeof value.credential !== 'string' || value.credential.length < 1 || value.credential.length > 1024) {
    throw new TypeError('TURN credential is invalid')
  }
  return [{ urls, username: value.username, credential: value.credential }]
}

function receivedBytes(value) {
  if (value instanceof Uint8Array) return value
  if (value instanceof ArrayBuffer) return new Uint8Array(value)
  if (ArrayBuffer.isView(value)) return new Uint8Array(value.buffer, value.byteOffset, value.byteLength)
  return null
}

async function defaultModuleLoader() {
  return import('./vendor-roomhash/trystero-torrent.mjs')
}

// This is a single-action RoomHash transport with no directory or ambient app API.
export async function createBrowserRoomHashTransport(options = {}) {
  exactObject(options, ['joinRoom', 'moduleLoader', 'trackerUrls', 'turn'], 'browser RoomHash options')
  const trackers = trackerUrls(options.trackerUrls ?? DEFAULT_TRACKERS)
  const turns = turnConfig(options.turn ?? null)
  let joinRoom = options.joinRoom
  if (joinRoom === undefined) {
    const module = await (options.moduleLoader ?? defaultModuleLoader)()
    joinRoom = module?.joinRoom
  }
  if (typeof joinRoom !== 'function') throw new TypeError('Trystero joinRoom is required')
  const sessions = new Set()

  return Object.freeze({
    async join(input) {
      exactObject(input, ['channelId', 'onMessage'], 'RoomHash join')
      if (!validChannelId(input.channelId)) throw new Error('RoomHash channelId must be a non-nil RFC 4122 UUID')
      if (typeof input.onMessage !== 'function') throw new TypeError('RoomHash onMessage callback is required')
      const config = { appId: APP_ID, relayConfig: { urls: trackers } }
      if (turns.length) config.turnConfig = turns
      const room = joinRoom(config, input.channelId)
      if (!room || typeof room.makeAction !== 'function' || typeof room.leave !== 'function') {
        throw new TypeError('Trystero returned an invalid room')
      }
      const action = room.makeAction(ACTION)
      if (!action || typeof action.send !== 'function') throw new TypeError('Trystero returned an invalid action')
      const peers = new Set()
      let closed = false
      const session = Object.freeze({
        async send(message) {
          if (closed) throw new Error('RoomHash collaboration transport is closed')
          const bytes = receivedBytes(message)
          if (!bytes || bytes.byteLength < 1 || bytes.byteLength > MAX_MESSAGE_BYTES) {
            throw new RangeError(`message must contain 1..${MAX_MESSAGE_BYTES} bytes`)
          }
          await action.send(bytes.slice())
        },
        close() {
          if (closed) return
          closed = true
          sessions.delete(session)
          peers.clear()
          room.leave()
        },
        status() {
          return Object.freeze({ status: closed ? 'closed' : 'joined', peer_count: peers.size })
        }
      })
      action.onMessage = (message) => {
        if (closed) return false
        const bytes = receivedBytes(message)
        if (!bytes || bytes.byteLength < 1 || bytes.byteLength > MAX_MESSAGE_BYTES) return false
        return input.onMessage(bytes.slice()) === true
      }
      room.onPeerJoin = (peerId) => {
        if (!closed && typeof peerId === 'string' && peerId.length <= 256) peers.add(peerId)
      }
      room.onPeerLeave = (peerId) => peers.delete(peerId)
      sessions.add(session)
      return session
    },
    async close() {
      for (const session of [...sessions]) session.close()
    }
  })
}
