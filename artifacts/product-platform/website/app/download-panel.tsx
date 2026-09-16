'use client';

import { useEffect, useRef, useState } from 'react';

const preference = 'vibapp.download.http-threshold-mib';
const hash = async (bytes: ArrayBuffer) => Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), n => n.toString(16).padStart(2, '0')).join('');
type FileSpec = { path: string; sha256: string; sizeBytes: number };
type Locator = { appId: string; packageDigestSha256: string; sizeBytes: number; candidate: Record<string, unknown> & { files?: FileSpec[] } };
type Store = { readFile: (digest: string, path: string) => Promise<Uint8Array>; getReceipt: (digest: string) => Promise<unknown>; begin: (plan: unknown) => Promise<any> };
type Job = { appId: string; state: string; received: number; total: number; method: string; error?: string };

// This handle never enters the guest Worker or app state. Only public, verified
// immutable packages may be read/written, under a dedicated cache directory.
async function rememberDirectory(value?: any) {
  const database = await new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open('vibapp-shared-package-directory-v1', 1);
    request.onupgradeneeded = () => request.result.createObjectStore('preferences');
    request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
  });
  try {
    return await new Promise<any>((resolve, reject) => {
      const tx = database.transaction('preferences', value ? 'readwrite' : 'readonly');
      const request = value ? tx.objectStore('preferences').put(value, 'directory') : tx.objectStore('preferences').get('directory');
      tx.oncomplete = () => resolve(request.result); tx.onerror = () => reject(tx.error); tx.onabort = () => reject(tx.error);
    });
  } finally { database.close(); }
}

async function packageFile(root: any, digest: string, path: string, create: boolean) {
  if (!/^[0-9a-f]{64}$/.test(digest) || !/^(candidate\.json|package\/[A-Za-z0-9._/-]+)$/.test(path)
    || path.split('/').some(part => ['', '.', '..'].includes(part))) throw new Error('Invalid cache path');
  let directory = await root.getDirectoryHandle(digest + '.vibapp-candidate', { create });
  const parts = path.split('/');
  for (const part of parts.slice(0, -1)) directory = await directory.getDirectoryHandle(part, { create });
  return directory.getFileHandle(parts.at(-1), { create });
}

async function importShared(root: any, locator: Locator, store: Store, signal: AbortSignal) {
  const files = locator.candidate.files!;
  const stage = await store.begin({ packageDigestSha256: locator.packageDigestSha256, sizeBytes: locator.sizeBytes, files });
  if (stage.alreadyCommitted) return stage.commit();
  try {
    for (const spec of files) {
      signal.throwIfAborted();
      const file = await (await packageFile(root, locator.packageDigestSha256, spec.path, false)).getFile();
      if (file.size !== spec.sizeBytes) throw new Error('Cache size mismatch');
      await stage.writeFile({ ...spec, bytes: await file.arrayBuffer() });
    }
    signal.throwIfAborted();
    return await stage.commit();
  } catch (error) { await stage.abort(); throw error; }
}

async function exportShared(root: any, locator: Locator, store: Store, signal: AbortSignal) {
  // candidate.json is the commit marker; write it last. Native import still
  // verifies every file in a private snapshot before trusting this cache.
  const files = [...locator.candidate.files!].sort((a, b) => Number(a.path === 'candidate.json') - Number(b.path === 'candidate.json'));
  for (const spec of files) {
    signal.throwIfAborted();
    const bytes = await store.readFile(locator.packageDigestSha256, spec.path);
    if (bytes.byteLength !== spec.sizeBytes || await hash(bytes as unknown as ArrayBuffer) !== spec.sha256) throw new Error('Cache integrity mismatch');
    const writable = await (await packageFile(root, locator.packageDigestSha256, spec.path, true)).createWritable();
    try { await writable.write(bytes); signal.throwIfAborted(); await writable.close(); }
    catch (error) { await writable.abort().catch(() => {}); throw error; }
  }
}

export function useAppDownloads() {
  const [job, setJob] = useState<Job | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [note, setNote] = useState('');
  const [threshold, setThreshold] = useState(20);
  const [p2p, setP2p] = useState(false);
  const directory = useRef<any>(null);
  const active = useRef<AbortController | null>(null);
  const decision = useRef<((choice: string) => void) | null>(null);
  const initialized = useRef(false);
  const [zh, setZh] = useState(false);
  useEffect(() => {
    const update = () => setZh(document.documentElement.lang.startsWith('zh'));
    update();
    try {
      const saved = Number(localStorage.getItem(preference) || 20);
      if (saved >= 0.1 && saved <= 64) setThreshold(saved);
      setP2p(localStorage.getItem('vibapp.download.p2p') === 'true');
    } catch {}
    const observer = new MutationObserver(update);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['lang'] });
    const cancel = () => active.current?.abort(new DOMException('Page closed', 'AbortError'));
    window.addEventListener('pagehide', cancel);
    return () => { observer.disconnect(); window.removeEventListener('pagehide', cancel); cancel(); };
  }, []);
  const t = (cn: string, en: string) => zh ? cn : en;
  const text = {
    preparing: t('准备下载', 'Preparing'), directory: t('选择缓存位置', 'Choose cache location'),
    downloading: t('正在下载', 'Downloading'), verifying: t('正在校验', 'Verifying'),
    complete: t('已就绪', 'Ready'), error: t('下载未完成', 'Download interrupted'), cancelled: t('已取消', 'Cancelled'),
  };
  const choose = async () => {
    try {
      // Must be called directly by a user gesture, never from the broker.
      const picker = (window as any).showDirectoryPicker;
      if (!picker) { setNote(t('已使用浏览器缓存。使用 Chrome 可选择与客户端共享的目录。', 'Using browser cache. Use Chrome to share a folder with the desktop client.')); decision.current?.('browser'); return; }
      const handle = await picker({ id: 'vibapp-public-packages', mode: 'readwrite', startIn: 'downloads' });
      directory.current = handle;
      await rememberDirectory(handle).catch(() => {});
      setNote(t('共享目录：', 'Shared folder: ') + handle.name);
      decision.current?.('shared');
    } catch { setNote(t('未选择目录，可以继续使用浏览器缓存。', 'No folder selected. You can continue with browser cache.')); }
  };
  const waitChoice = (signal: AbortSignal) => new Promise<string>((resolve, reject) => {
    const abort = () => { decision.current = null; reject(signal.reason); };
    signal.throwIfAborted(); signal.addEventListener('abort', abort, { once: true });
    decision.current = choice => { decision.current = null; signal.removeEventListener('abort', abort); resolve(choice); };
  });
  const manager = useRef<any>(null);
  manager.current = {
    cancel: () => active.current?.abort(new DOMException('Download cancelled', 'AbortError')),
    async acquire(locator: Locator, controller: { store: Store; node: any }, settings: Record<string, unknown>) {
      if (active.current) throw new Error('Download already active');
      const abort = new AbortController(); active.current = abort;
      const signal = abort.signal;
      let current: Job = { appId: locator.appId, state: 'preparing', received: 0, total: locator.sizeBytes, method: 'HTTP' };
      const update = (next: Partial<Job>) => { current = { ...current, ...next }; setJob(current); };
      update({});
      try {
        let limit = 20;
        try { limit = Number(localStorage.getItem(preference) || 20); } catch {}
        if (!Number.isFinite(limit) || limit < 0.1 || limit > 64) limit = 20;
        setThreshold(limit);
        let useP2p = settings.p2pEnabled === true;
        try {
          const saved = localStorage.getItem('vibapp.download.p2p');
          if (saved === 'true' || saved === 'false') useP2p = saved === 'true';
        } catch {}
        setP2p(useP2p);
        if (!initialized.current) {
          initialized.current = true;
          const remembered = await rememberDirectory().catch(() => null);
          if (remembered && await remembered.queryPermission({ mode: 'readwrite' }) === 'granted') directory.current = remembered;
        }
        const cached = await controller.store.getReceipt(locator.packageDigestSha256);
        if (cached) {
          if (directory.current) {
            try { await exportShared(directory.current, locator, controller.store, signal); }
            catch { signal.throwIfAborted(); setNote(t('共享目录不可用，继续使用浏览器缓存。', 'Shared folder unavailable. Using browser cache.')); }
          }
          update({ state: 'complete', received: locator.sizeBytes, method: 'Cache' }); return cached;
        }
        if (locator.sizeBytes > limit * 1024 * 1024 && !directory.current) {
          setExpanded(true);
          if (typeof (window as any).showDirectoryPicker === 'function') {
            update({ state: 'directory' }); await waitChoice(signal);
          } else setNote(t('已使用浏览器缓存。使用 Chrome 可选择共享目录。', 'Using browser cache. Use Chrome to choose a shared folder.'));
        }
        if (directory.current) {
          try {
            const receipt = await importShared(directory.current, locator, controller.store, signal);
            update({ state: 'complete', received: locator.sizeBytes, method: 'Cache' }); return receipt;
          } catch { signal.throwIfAborted(); /* Missing/tampered cache is not authority. */ }
        }
        for (;;) {
          signal.throwIfAborted();
          try {
            const input = { ...locator.candidate, signal, onProgress: (received: number) => update({ received: Math.min(locator.sizeBytes, received) }) };
            let receipt;
            if (locator.sizeBytes > limit * 1024 * 1024 && useP2p) {
              update({ state: 'downloading', method: 'P2P + HTTP', received: 0 });
              try {
                await controller.node.start({ ...settings, p2pEnabled: true });
                receipt = await controller.node.fetchVerifiedCandidateToStore({ ...input, timeoutMs: 15_000 });
              } catch { signal.throwIfAborted(); update({ method: 'HTTP', received: 0 }); }
            }
            if (!receipt) {
              update({ state: 'downloading', method: 'HTTP', received: 0 });
              receipt = await controller.node.fetchHttpCandidateToStore(input);
            }
            signal.throwIfAborted(); update({ state: 'verifying', received: locator.sizeBytes });
            if (directory.current) {
              try { await exportShared(directory.current, locator, controller.store, signal); }
              catch { signal.throwIfAborted(); setNote(t('共享目录无法写入，已保存在浏览器缓存。', 'Could not write to the shared folder. Saved in browser cache.')); }
            }
            update({ state: 'complete' }); return receipt;
          } catch (error) {
            signal.throwIfAborted(); update({ state: 'error', error: String(error).slice(0, 180) }); setExpanded(true);
            await waitChoice(signal);
          }
        }
      } catch (error) { update({ state: 'cancelled' }); throw error; }
      finally { active.current = null; decision.current = null; }
    },
  };
  const panel = <aside className="app-downloads" aria-label={t('下载', 'Downloads')}>
    <button className="downloads-toggle" onClick={() => setExpanded(!expanded)} aria-expanded={expanded} title={t('下载', 'Downloads')}>↓{job && !['complete', 'cancelled'].includes(job.state) ? ' ·' : ''}</button>
    {expanded && <section className="downloads-popover">
      <h2>{t('下载', 'Downloads')}</h2>
      {job ? <div aria-live="polite"><strong>{text[job.state as keyof typeof text]}</strong><p className="download-app-id">{job.appId}</p>
        <progress max={job.total} value={job.received} aria-label={t('下载进度', 'Download progress')} />
        <p>{(job.received / 1048576).toFixed(1)} / {(job.total / 1048576).toFixed(1)} MB · {job.method}</p>
        {job.state === 'directory' && <><p>{t('推荐选择 Downloads/VibApp/Packages。客户端使用相同目录即可复用应用包。', 'Choose Downloads/VibApp/Packages to reuse packages with the desktop client.')}</p><button onClick={choose}>{t('选择目录', 'Choose folder')}</button><button onClick={() => decision.current?.('browser')}>{t('使用浏览器缓存', 'Use browser cache')}</button></>}
        {job.state === 'error' && <><p role="alert">{t('请检查网络后重试。', 'Check your connection and retry.')}</p><details><summary>{t('详情', 'Details')}</summary>{job.error}</details><button onClick={() => decision.current?.('retry')}>{t('重试', 'Retry')}</button></>}
        {!['complete', 'cancelled'].includes(job.state) && <button onClick={() => manager.current.cancel()}>{t('取消', 'Cancel')}</button>}
      </div> : <p>{t('暂无下载', 'No downloads yet')}</p>}
      <details><summary>{t('下载设置', 'Download settings')}</summary>
        <label>{t('HTTP 优先，小于（MB）', 'Prefer HTTP below (MB)')}<input type="number" min="0.1" step="0.1" max="64" value={threshold} onChange={event => {
          const n = Number(event.target.value); if (Number.isFinite(n) && n >= 0.1 && n <= 64) { setThreshold(n); try { localStorage.setItem(preference, String(n)); } catch {} }
        }} /></label><button onClick={choose}>{t('共享缓存目录…', 'Shared cache folder…')}</button>
        <label><input type="checkbox" checked={p2p} onChange={event => {
          setP2p(event.target.checked); try { localStorage.setItem('vibapp.download.p2p', String(event.target.checked)); } catch {}
        }} />{t('大应用使用 P2P 下载和分享', 'Use P2P to download and share large apps')}</label>
        <p>{t('仅共享应用安装包，不包含账号或应用私有数据。P2P 与 RTC 基于 RoomHash 网络。', 'Only app packages are shared, never accounts or private app data. P2P and RTC use the RoomHash network.')}</p>
      </details>{note && <p role="status">{note}</p>}
    </section>}
  </aside>;
  return { manager, panel };
}
