'use client';

import { useLayoutEffect, useMemo, useRef, useState } from 'react';
import { buildG2Skeleton, type Vec3 } from '../lib/g2Kinematics';
import type { JointTargets } from '../lib/teleop';

const LIGHT_COLORS = { base: '#8d9cad', body: '#72a7d1', head: '#9b91c8', left: '#59bbaa', right: '#df9874' };
const DARK_COLORS = { base: '#b8c5d3', body: '#8fc7f2', head: '#c1b6ea', left: '#79d8c6', right: '#f2b28c' };

export function G2RobotPreview({ joints, live, darkMode }: { joints: JointTargets; live: boolean; darkMode: boolean }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const dragRef = useRef<{ x: number; y: number; pan: boolean } | null>(null);
  const [view, setView] = useState({ yaw: -0.65, pitch: 0.28, zoom: 1, panX: 0, panY: 0 });
  const [canvasSize, setCanvasSize] = useState({ width: 0, height: 0 });
  const skeleton = useMemo(() => buildG2Skeleton(joints), [joints]);

  useLayoutEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => {
      const width = canvas.clientWidth, height = canvas.clientHeight;
      setCanvasSize((previous) => previous.width === width && previous.height === height ? previous : { width, height });
    });
    observer.observe(canvas);
    return () => observer.disconnect();
  }, []);

  useLayoutEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || navigator.userAgent.includes('jsdom')) return;
    const cssWidth = canvas.clientWidth || 560; const cssHeight = canvas.clientHeight || 330;
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(cssWidth * ratio); canvas.height = Math.round(cssHeight * ratio);
    const context = canvas.getContext('2d');
    if (!context) return;
    const colors = darkMode ? DARK_COLORS : LIGHT_COLORS;
    context.scale(ratio, ratio); context.clearRect(0, 0, cssWidth, cssHeight);
    const cosineYaw = Math.cos(view.yaw); const sineYaw = Math.sin(view.yaw);
    const cosinePitch = Math.cos(view.pitch); const sinePitch = Math.sin(view.pitch);
    const project = (point: Vec3) => {
      const x1 = cosineYaw * point.x - sineYaw * point.y; const y1 = sineYaw * point.x + cosineYaw * point.y;
      const z2 = sinePitch * y1 + cosinePitch * point.z; const scale = 155 * view.zoom;
      return { x: cssWidth / 2 + view.panX + x1 * scale, y: cssHeight * 0.86 + view.panY - z2 * scale, depth: cosinePitch * y1 - sinePitch * point.z };
    };
    context.strokeStyle = darkMode ? 'rgba(205, 216, 230, .12)' : 'rgba(92, 108, 128, .14)'; context.lineWidth = 1;
    for (let index = -4; index <= 4; index += 1) {
      const a = project({ x: index * 0.2, y: -0.8, z: 0 }); const b = project({ x: index * 0.2, y: 0.8, z: 0 });
      const c = project({ x: -0.8, y: index * 0.2, z: 0 }); const d = project({ x: 0.8, y: index * 0.2, z: 0 });
      context.beginPath(); context.moveTo(a.x, a.y); context.lineTo(b.x, b.y); context.stroke();
      context.beginPath(); context.moveTo(c.x, c.y); context.lineTo(d.x, d.y); context.stroke();
    }
    const rendered = skeleton.segments.map((segment) => ({ segment, from: project(skeleton.points[segment.from]), to: project(skeleton.points[segment.to]) }))
      .sort((a, b) => (a.from.depth + a.to.depth) - (b.from.depth + b.to.depth));
    context.lineCap = 'round';
    rendered.forEach(({ segment, from, to }) => {
      context.strokeStyle = darkMode ? 'rgba(10, 15, 24, .48)' : 'rgba(255, 255, 255, .86)';
      context.lineWidth = segment.group === 'base' ? 6 : 9;
      context.beginPath(); context.moveTo(from.x, from.y); context.lineTo(to.x, to.y); context.stroke();
    });
    rendered.forEach(({ segment, from, to }) => {
      context.strokeStyle = colors[segment.group]; context.lineWidth = segment.group === 'base' ? 3.5 : 5.5; context.lineCap = 'round';
      context.shadowColor = `${colors[segment.group]}55`; context.shadowBlur = darkMode ? 6 : 3;
      context.beginPath(); context.moveTo(from.x, from.y); context.lineTo(to.x, to.y); context.stroke();
    });
    context.shadowBlur = 0;
    rendered.forEach(({ segment, to }) => {
      context.fillStyle = colors[segment.group]; context.strokeStyle = darkMode ? 'rgba(15, 22, 34, .56)' : 'rgba(255, 255, 255, .9)'; context.lineWidth = 1.5;
      context.beginPath(); context.arc(to.x, to.y, segment.group === 'base' ? 3 : 4.5, 0, Math.PI * 2); context.fill(); context.stroke();
    });
    context.font = '600 11px Ubuntu, "Noto Sans CJK SC", sans-serif'; context.fillStyle = live ? (darkMode ? '#79d8ad' : '#3c9f78') : (darkMode ? '#aebdce' : '#76879a');
    context.beginPath(); context.arc(18, 19, 4, 0, Math.PI * 2); context.fill(); context.fillText(live ? 'LIVE' : 'TARGET', 29, 23);
  }, [skeleton, view, live, darkMode, canvasSize]);

  const changeZoom = (factor: number) => setView((current) => ({ ...current, zoom: Math.max(0.65, Math.min(1.65, current.zoom * factor)) }));
  return (
    <div className="teleop-robot-preview">
      <div className="teleop-robot-zoom" aria-label="姿态视图缩放">
        <button type="button" aria-label="缩小机器人姿态" onClick={() => changeZoom(0.88)}>−</button>
        <button type="button" aria-label="放大机器人姿态" onClick={() => changeZoom(1.12)}>+</button>
        <button type="button" aria-label="恢复机器人视角" onClick={() => setView({ yaw: -0.65, pitch: 0.28, zoom: 1, panX: 0, panY: 0 })}>↺</button>
      </div>
      <canvas ref={canvasRef} className="teleop-robot-canvas" role="img" aria-label="G2 全身 3D 关节姿态" data-model-source="G2_t2_crs.urdf"
        onContextMenu={(event) => event.preventDefault()}
        onLostPointerCapture={() => { dragRef.current = null; }}
        onPointerDown={(event) => { if (event.button !== 0 && event.button !== 2) return; event.preventDefault(); dragRef.current = { x: event.clientX, y: event.clientY, pan: event.button === 2 }; event.currentTarget.setPointerCapture?.(event.pointerId); }}
        onPointerMove={(event) => { if (!dragRef.current) return; const dx = event.clientX - dragRef.current.x; const dy = event.clientY - dragRef.current.y; const pan = dragRef.current.pan; dragRef.current = { ...dragRef.current, x: event.clientX, y: event.clientY }; setView((current) => pan ? { ...current, panX: current.panX + dx, panY: current.panY + dy } : ({ ...current, yaw: current.yaw + dx * 0.008, pitch: Math.max(-0.5, Math.min(0.8, current.pitch + dy * 0.006)) })); }}
        onPointerUp={() => { dragRef.current = null; }} onPointerCancel={() => { dragRef.current = null; }} />
    </div>
  );
}
