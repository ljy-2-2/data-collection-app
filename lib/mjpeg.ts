/** Decode JPEG frames without the WebKit native multipart image loader.
 * That loader can retain closed connections after a preflight -> monitor switch.
 */
export class MjpegDecoder {
  private pending = new Uint8Array(0);

  push(chunk: Uint8Array): Uint8Array<ArrayBuffer> | null {
    const bytes = new Uint8Array(this.pending.length + chunk.length);
    bytes.set(this.pending);
    bytes.set(chunk, this.pending.length);
    let start = -1;
    let latest: Uint8Array<ArrayBuffer> | null = null;
    for (let index = 0; index + 1 < bytes.length; index += 1) {
      if (bytes[index] !== 0xff) continue;
      if (start < 0 && bytes[index + 1] === 0xd8) {
        start = index;
        index += 1;
      } else if (start >= 0 && bytes[index + 1] === 0xd9) {
        latest = bytes.slice(start, index + 2);
        start = -1;
        index += 1;
      }
    }
    this.pending = start >= 0 ? bytes.slice(start)
      : bytes.at(-1) === 0xff ? bytes.slice(-1) : new Uint8Array(0);
    if (this.pending.length > 8 * 1024 * 1024) throw new Error('相机帧格式异常');
    return latest;
  }
}

export async function readMjpeg(
  url: string,
  signal: AbortSignal,
  onFrame: (frame: Uint8Array<ArrayBuffer>) => void,
): Promise<void> {
  // octet-stream also bypasses WebKit's multipart network handler for fetch.
  const separator = url.includes('?') ? '&' : '?';
  const response = await fetch(`${url}${separator}raw=1`, { signal, cache: 'no-store' });
  if (!response.ok || !response.body) throw new Error(`相机请求失败 (${response.status})`);
  const reader = response.body.getReader();
  const decoder = new MjpegDecoder();
  try {
    while (!signal.aborted) {
      const { done, value } = await reader.read();
      if (done) throw new Error('相机视频流已断开');
      const frame = decoder.push(value);
      if (frame) onFrame(frame);
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
