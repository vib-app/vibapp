import type { Metadata } from 'next';
import './globals.css';

const siteOrigin = process.env.NEXT_PUBLIC_SITE_ORIGIN ?? 'http://localhost:3000';

export const metadata: Metadata = {
  metadataBase: new URL(siteOrigin),
  title: 'VibApp — WebAssembly Client',
  description: 'VibApp Client 的 WebAssembly 版本：搜索、需求对话、开发任务、应用与设置使用同一套 GUI。',
  icons: { icon: '/favicon.svg' },
  openGraph: {
    title: 'VibApp — WebAssembly Client',
    description: '同一套 VibApp GUI，运行在浏览器中。',
    images: [{ url: '/og.png', width: 1672, height: 941, alt: 'VibApp WebAssembly Client' }],
  },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body>{children}</body>
    </html>
  );
}
