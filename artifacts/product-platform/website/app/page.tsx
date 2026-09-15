import PreviewFrame from './preview-frame';
import { getPreviewOrigin } from '../lib/preview-origin';

export default function VibAppWebClient() {
  const hostedShell = process.env.NEXT_PUBLIC_VIBAPP_HOSTED_SHELL === '1';
  return (
    <main className={`web-client-host${hostedShell ? ' hosted-shell' : ''}`}>
      <PreviewFrame previewOrigin={getPreviewOrigin()} title="VibApp Web GUI" trustedShellOnly={hostedShell} />
      <noscript>VibApp needs JavaScript and WebAssembly. / VibApp 需要启用 JavaScript 和 WebAssembly。</noscript>
    </main>
  );
}
