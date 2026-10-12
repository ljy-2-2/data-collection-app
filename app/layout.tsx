import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: '小凯实验室数采平台',
  description: '机器人遥操作与 UMI 数据采集工作台',
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <head><script src="/runtime-config.js" /></head>
      <body>{children}</body>
    </html>
  );
}
