'use client';
import { useEffect, useRef, useState } from 'react';
import { readMjpeg } from '../lib/mjpeg';

const umiCameraCancellations = new Set<() => void>();

export function releaseUmiCameraStreams() {
  for (const cancel of umiCameraCancellations) cancel();
  umiCameraCancellations.clear();
}

export function CameraStream({
  src,
  alt,
  scope = 'umi',
  waiting = false,
  refreshTimeoutMs = 10000,
}: {
  src: string;
  alt: string;
  scope?: 'umi' | 'tianji';
  waiting?: boolean;
  refreshTimeoutMs?: number;
}) {
  const [attempt, setAttempt] = useState(0);
  const [failed, setFailed] = useState(false);
  const [stalled, setStalled] = useState(false);
  const [hasFrame, setHasFrame] = useState(false);
  const imageRef = useRef<HTMLImageElement>(null);
  const objectUrlRef = useRef('');

  // Reconnect attempts retain the last decoded frame. A camera/source change
  // releases it, so a wrist switch can never display the other camera's image.
  useEffect(() => {
    setHasFrame(false);
    const element = imageRef.current;
    return () => {
      element?.removeAttribute('src');
      if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
      objectUrlRef.current = '';
    };
  }, [src, scope]);

  useEffect(() => {
    const element = imageRef.current;
    if (!element) return;
    const controller = new AbortController();
    let disposed = false;
    let animation = 0;
    let latest: Uint8Array<ArrayBuffer> | null = null;
    let timeout = window.setTimeout(() => controller.abort(), refreshTimeoutMs);
    let staleTimer = window.setTimeout(() => setStalled(true), 500);
    setFailed(false);
    const cancel = () => {
      disposed = true;
      controller.abort();
      window.clearTimeout(timeout);
      window.clearTimeout(staleTimer);
      window.cancelAnimationFrame(animation);
      if (scope === 'umi') {
        element.removeAttribute('src');
        if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
        objectUrlRef.current = '';
      }
      umiCameraCancellations.delete(cancel);
    };
    if (scope === 'umi') umiCameraCancellations.add(cancel);
    void readMjpeg(src, controller.signal, (frame) => {
      if (disposed) return;
      window.clearTimeout(timeout);
      window.clearTimeout(staleTimer);
      setStalled(false);
      staleTimer = window.setTimeout(() => setStalled(true), 500);
      timeout = window.setTimeout(() => controller.abort(), refreshTimeoutMs);
      latest = frame;
      if (animation) return;
      animation = window.requestAnimationFrame(() => {
        animation = 0;
        if (disposed || !latest) return;
        const previous = objectUrlRef.current;
        objectUrlRef.current = URL.createObjectURL(
          new Blob([latest], { type: 'image/jpeg' }),
        );
        element.src = objectUrlRef.current;
        setHasFrame(true);
        if (previous) URL.revokeObjectURL(previous);
      });
    }).catch(() => {
      if (!disposed) {
        controller.abort();
        setFailed(true);
      }
    });
    return cancel;
  }, [src, attempt, scope, refreshTimeoutMs]);

  useEffect(() => {
    if (!failed) return;
    const timer = window.setTimeout(() => {
      setAttempt((value) => value + 1);
      setFailed(false);
    }, 800);
    return () => window.clearTimeout(timer);
  }, [failed]);

  return (
    <>
      <img
        ref={imageRef}
        className={failed && scope !== 'tianji' ? 'is-stream-error' : ''}
        alt={alt}
      />
      {scope === 'tianji' ? (
        !hasFrame && (waiting || stalled || failed) ? (
          <span className="umi-stream-retry">等待相机画面…</span>
        ) : null
      ) : failed ? <span className="umi-stream-retry">视频重连中…</span> : null}
    </>
  );
}
