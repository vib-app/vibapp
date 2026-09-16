import type { NextConfig } from 'next';
import { getPreviewOrigin } from './lib/preview-origin';

const previewOrigin = getPreviewOrigin();
const hostedShell = process.env.NEXT_PUBLIC_VIBAPP_HOSTED_SHELL === '1';

const nextConfig: NextConfig = {
  async headers() {
    const headers = [{
      source: '/(.*)',
      headers: [
        {
          key: 'Content-Security-Policy',
          value: "default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'; worker-src 'self'; connect-src 'self' https://raw.githubusercontent.com wss://tracker.webtorrent.dev wss://tracker.openwebtorrent.com wss://tracker.btorrent.xyz; frame-src " + (hostedShell ? "'self'" : previewOrigin) + "; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'; manifest-src 'self'",
        },
        { key: 'Cross-Origin-Opener-Policy', value: 'same-origin' },
        { key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' },
        { key: 'X-Content-Type-Options', value: 'nosniff' },
        {
          key: 'Permissions-Policy',
          value: 'camera=(), microphone=(), geolocation=(), payment=(), usb=(), serial=(), clipboard-read=(), clipboard-write=()',
        },
      ],
    }];
    if (hostedShell) headers.push({
      source: '/launcher/:path*',
      headers: [{
        key: 'Content-Security-Policy',
        value: "default-src 'none'; img-src 'self' data:; style-src 'self'; script-src 'self' 'wasm-unsafe-eval'; connect-src 'self'; worker-src 'self'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'",
      }],
    });
    return headers;
  },
};

export default nextConfig;
