"""Loopback-only, credential-free browser probe for a public release receipt."""
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import time

BASE = Path(__file__).resolve().parent
HTML = r'''<!doctype html><meta charset="utf-8"><title>VibApp GitHub webseed verification</title>
<style>body{font:16px system-ui;max-width:1000px;margin:40px auto;background:#122019;color:#e1f6e9}pre{white-space:pre-wrap;overflow-wrap:anywhere;padding:20px;background:#1e3025;border-radius:12px}h1{font-size:24px}</style>
<h1>VibApp · GitHub Raw / WebTorrent 验证</h1><p>禁用 Tracker 和 P2P 节点，只使用公开 HTTP 下载源。</p><pre id="result">准备验证…</pre>
<script type="module">
import WebTorrent from '/webtorrent.js';
const display = document.querySelector('#result');
window.probe = {state: 'running'};
const show = value => { window.probe = value; display.textContent = JSON.stringify(value,null,2); };
const hash = async data => [...new Uint8Array(await crypto.subtle.digest('SHA-256',data))].map(b=>b.toString(16).padStart(2,'0')).join('');
const require = (condition, code) => {if(!condition) throw new Error(code)};
let client;
try {
  const receipt = await (await fetch('/receipt.json')).json();
  const metadata = await fetch(receipt.torrent_url);
  require(metadata.ok, 'torrent-http');
  const torrentBytes = new Uint8Array(await metadata.arrayBuffer());
  const expected = receipt.verified_downloads.filter(f => f.path === 'candidate.json' || f.path?.startsWith('package/'));
  require(expected.length === 5, 'five-package-files');
  const component = expected.find(f => f.path === 'package/component.wasm');
  const range = await fetch(component.url, {headers: {Range:'bytes=0-63'}});
  const prefix = new Uint8Array(await range.arrayBuffer());
  require(range.status === 206 && prefix.length === 64, 'http-range-206');
  client = new WebTorrent({dht:false, lsd:false, tracker:false});
  const wires = [];
  const downloaded = await new Promise((resolve,reject) => {
    const deadline = setTimeout(() => reject(new Error('webseed-timeout')), 120000);
    const fail = error => {clearTimeout(deadline);reject(error)};
    client.on('error', fail);
    const torrent = client.add(torrentBytes, {announce:[]});
    torrent.on('error',fail);
    torrent.on('wire', wire => {wires.push(wire.type);if(wire.type !== 'webSeed') fail(new Error('unexpected-peer-wire'));});
    torrent.on('done', () => {clearTimeout(deadline);resolve(torrent)});
  });
  require(downloaded.infoHash === receipt.info_hash,'torrent-info-hash');
  require(wires.length > 0 && wires.every(type => type === 'webSeed'), 'webseed-only');
  const files = [];
  require(downloaded.files.length === expected.length,'torrent-file-count');
  for (const file of downloaded.files) {
    const relative = file.path.slice(downloaded.name.length+1);
    const wanted = expected.find(f=>f.path===relative);
    require(Boolean(wanted), 'torrent-file-path');
    const bytes = new Uint8Array(await file.arrayBuffer());
    const digest = await hash(bytes);
    require(bytes.length===wanted.size_bytes && digest===wanted.sha256, 'torrent-file-sha256');
    if(relative==='package/component.wasm') require(prefix.every((b,i)=>bytes[i]===b),'range-prefix-bytes');
    files.push({path:relative,size_bytes:bytes.length,sha256:digest});
  }
  show({state:'passed',package_digest_sha256:receipt.package_digest_sha256,info_hash:downloaded.infoHash,
    tracker:false,peer_connections:0,wire_types:wires,range_status:range.status,range_bytes:prefix.length,
    cors:'browser cross-origin fetch and WebTorrent succeeded',downloaded:downloaded.downloaded,files});
} catch(error) {show({state:'failed',error:String(error)});} finally {if(client) client.destroy();}
</script>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    raw = args.receipt.read_bytes()
    if len(raw) > 1048576:
        raise ValueError('receipt-size')
    receipt = json.loads(raw)
    # Serve only the required public fields, never the filesystem/job ledger.
    data = json.dumps({k: receipt[k] for k in (
        'package_digest_sha256', 'torrent_url', 'info_hash', 'verified_downloads')}).encode()
    library = (BASE.parent / 'web-client-core/vendor-roomhash/webtorrent.min.js').read_bytes()
    allowed = {'/': ('text/html; charset=utf-8', HTML.encode()),
               '/webtorrent.js': ('text/javascript', library),
               '/receipt.json': ('application/json', data)}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}' or self.path not in allowed:
                self.send_error(404)
                return
            mime, payload = allowed[self.path]
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(payload)

    with HTTPServer(('127.0.0.1', args.port), Handler) as server:
        print(json.dumps({'url': f'http://127.0.0.1:{server.server_port}', 'ttl_seconds':600}), flush=True)
        server.timeout = 1
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            server.handle_request()


if __name__ == '__main__':
    main()
