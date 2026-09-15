import { stat } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

const APP_ID = 'roomhash-github-io-v2'
const ACTION = 'vibapp-collaboration-v1'

async function existingDirectory(path, label) {
  const absolute = resolve(path)
  if (absolute !== path) throw new Error(`${label} must be absolute`)
  if (!(await stat(absolute)).isDirectory()) throw new Error(`${label} must be a directory`)
  return absolute
}

// This factory uses RoomHash's pinned Trystero/Werift implementation, but not
// MeshNode: MeshNode deliberately advertises and crawls channel lists.
export async function createRoomHashTransport({ roomhash_root, config }) {
  const root = await existingDirectory(roomhash_root, 'roomhash_root')
  const torrentUrl = pathToFileURL(join(root, 'headless/node_modules/@trystero-p2p/torrent/dist/index.mjs')).href
  const weriftUrl = pathToFileURL(join(root, 'headless/node_modules/werift/lib/index.mjs')).href
  const rtcUrl = pathToFileURL(join(root, 'headless/src/rtc-config.js')).href
  const [{ joinRoom }, { RTCPeerConnection }, { createRtcConfig, managedPeerConnection }] = await Promise.all([
    import(torrentUrl),
    import(weriftUrl),
    import(rtcUrl)
  ])
  const rtcPeerConnection = managedPeerConnection(RTCPeerConnection, {
    name: 'vibapp-collaboration',
    maxActive: config.ice.mesh.maxActive,
    timeoutMs: config.ice.connectionTimeoutMs,
    log: config.log ?? (() => {})
  })
  const sessions = new Set()
  return {
    async join({ channelId, onMessage }) {
      const roomConfig = {
        appId: APP_ID,
        passive: true,
        rtcPolyfill: rtcPeerConnection,
        rtcConfig: createRtcConfig(config, 'mesh')
      }
      if (config.tracker && config.tracker !== 'wss://tracker.openwebtorrent.com') {
        roomConfig.relayConfig = { urls: [config.tracker] }
      }
      const room = joinRoom(roomConfig, channelId, config.joinOptions ?? {})
      const action = room.makeAction(ACTION)
      const peers = new Set()
      action.onMessage = (message) => {
        const bytes = message instanceof Uint8Array ? message : null
        if (bytes) onMessage(bytes)
      }
      room.onPeerJoin = (peerId) => peers.add(peerId)
      room.onPeerLeave = (peerId) => peers.delete(peerId)
      let closed = false
      const session = {
        send(message) {
          if (closed) throw new Error('RoomHash collaboration transport is closed')
          return action.send(message)
        },
        close() {
          if (closed) return
          closed = true
          sessions.delete(session)
          room.leave()
        },
        status() {
          return { status: closed ? 'closed' : 'joined', peer_count: peers.size }
        }
      }
      sessions.add(session)
      return session
    },
    async close() {
      for (const session of [...sessions]) session.close()
      await rtcPeerConnection.closeAll('vibapp-collaboration-shutdown')
    }
  }
}
