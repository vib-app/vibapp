const PRODUCTION_PREVIEW_ORIGIN = 'https://preview.vibapp.ai';

export function getPreviewOrigin(): string {
  const explicit = process.env.VIBAPP_PREVIEW_ORIGIN?.trim();
  const localBuild = process.env.VIBAPP_PREVIEW_LOCAL === '1';
  if (!explicit && process.env.NODE_ENV !== 'production') {
    throw new Error('VIBAPP_PREVIEW_ORIGIN is required outside production');
  }
  const configured = explicit || PRODUCTION_PREVIEW_ORIGIN;
  let parsed: URL;
  try {
    parsed = new URL(configured);
  } catch {
    throw new Error('VIBAPP_PREVIEW_ORIGIN must be an absolute origin');
  }
  if (parsed.username || parsed.password || parsed.pathname !== '/' || parsed.search || parsed.hash) {
    throw new Error('VIBAPP_PREVIEW_ORIGIN must not contain credentials, a path, query, or fragment');
  }
  const isProduction = parsed.origin === PRODUCTION_PREVIEW_ORIGIN;
  const isExplicitLoopback = parsed.protocol === 'http:' && parsed.hostname === '127.0.0.1' && parsed.port !== '';
  if (process.env.NODE_ENV === 'production' && !localBuild && !isProduction) {
    throw new Error('production VIBAPP_PREVIEW_ORIGIN must be exactly https://preview.vibapp.ai');
  }
  if (!isProduction && !isExplicitLoopback) {
    throw new Error('VIBAPP_PREVIEW_ORIGIN must be https://preview.vibapp.ai or an explicit 127.0.0.1 development port');
  }
  return parsed.origin;
}
