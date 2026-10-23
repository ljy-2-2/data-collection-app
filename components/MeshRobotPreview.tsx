'use client';
import { useEffect, useRef, useState } from 'react';
import {
  columnMajor,
  identity,
  linkTransforms,
  multiply,
  tianjiCameraMatrix,
  transform,
  type TianjiModel,
} from '../lib/tianjiModel';

const DEFAULT_VIEW = { yaw: -0.08, pitch: 0.03, zoom: 1 };

export function MeshRobotPreview({
  modelUrl, title, darkMode, anglesForModel, initialView=DEFAULT_VIEW,
}: {
  modelUrl:string; title:string; darkMode:boolean;
  anglesForModel:(model:TianjiModel)=>Record<string,number>;
  initialView?:{yaw:number;pitch:number;zoom:number};
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const draw = useRef<(() => void) | null>(null);
  const pose = useRef({ anglesForModel, darkMode });
  const view = useRef({ ...initialView });
  const drag = useRef<{ x: number; y: number } | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    pose.current = { anglesForModel, darkMode };
    draw.current?.();
  }, [anglesForModel, darkMode]);
  useEffect(() => {
    const el = canvas.current;
    if (!el) return;
    const abort = new AbortController();
    let cleanup = () => {};
    void fetch(modelUrl, { signal: abort.signal })
      .then((r) => {
        if (!r.ok) throw new Error('模型加载失败');
        return r.json() as Promise<TianjiModel>;
      })
      .then((model: TianjiModel) => {
        if (abort.signal.aborted) return;
        const gl = el.getContext('webgl', {
          alpha: false,
          depth: true,
          antialias: true,
        });
        if (!gl) throw new Error('当前设备不支持 3D 视图');
        const shader = (type: number, source: string) => {
          const s = gl.createShader(type)!;
          gl.shaderSource(s, source);
          gl.compileShader(s);
          if (!gl.getShaderParameter(s, gl.COMPILE_STATUS))
            throw new Error('3D 视图初始化失败');
          return s;
        };
        const program = gl.createProgram()!;
        const vs = shader(
          gl.VERTEX_SHADER,
          'attribute vec3 p; attribute vec3 n; uniform mat4 model; uniform mat4 camera; varying vec3 normal; void main(){ gl_Position=camera*model*vec4(p,1.); normal=mat3(model)*n; }',
        );
        const fs = shader(
          gl.FRAGMENT_SHADER,
          'precision mediump float; varying vec3 normal; uniform vec3 color; void main(){vec3 n=normalize(normal); if(!gl_FrontFacing) n=-n; float light=.32+.58*max(dot(n,normalize(vec3(-.6,-.4,1.))),0.)+.1*max(dot(n,normalize(vec3(.5,.6,.2))),0.);gl_FragColor=vec4(color*light,1.);}',
        );
        gl.attachShader(program, vs);
        gl.attachShader(program, fs);
        gl.linkProgram(program);
        if (!gl.getProgramParameter(program, gl.LINK_STATUS))
          throw new Error('3D 视图初始化失败');
        gl.useProgram(program);
        gl.enable(gl.DEPTH_TEST);
        const p = gl.getAttribLocation(program, 'p'),
          n = gl.getAttribLocation(program, 'n');
        const uModel = gl.getUniformLocation(program, 'model'),
          uCamera = gl.getUniformLocation(program, 'camera'),
          uColor = gl.getUniformLocation(program, 'color');
        const buffers = Object.fromEntries(
          Object.entries(model.meshes).map(([key, mesh]) => {
            const data: number[] = [];
            for (const f of mesh.faces) {
              const [a, b, c] = f.map((i) => mesh.vertices[i]);
              const u = b.map((v, i) => v - a[i]),
                v = c.map((v, i) => v - a[i]);
              const normal = [
                u[1] * v[2] - u[2] * v[1],
                u[2] * v[0] - u[0] * v[2],
                u[0] * v[1] - u[1] * v[0],
              ];
              const length = Math.hypot(...normal) || 1;
              for (const point of [a, b, c])
                data.push(...point, ...normal.map((x) => x / length));
            }
            const buffer = gl.createBuffer()!;
            gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
            gl.bufferData(
              gl.ARRAY_BUFFER,
              new Float32Array(data),
              gl.STATIC_DRAW,
            );
            return [key, { buffer, count: data.length / 6 }];
          }),
        );
        draw.current = () => {
          const ratio = Math.min(window.devicePixelRatio || 1, 2),
            w = el.clientWidth,
            h = el.clientHeight;
          if (!w || !h) return;
          const pixelWidth = Math.round(w * ratio),
            pixelHeight = Math.round(h * ratio);
          if (el.width !== pixelWidth || el.height !== pixelHeight) {
            el.width = pixelWidth;
            el.height = pixelHeight;
          }
          gl.viewport(0, 0, el.width, el.height);
          gl.enable(gl.DEPTH_TEST);
          gl.depthFunc(gl.LESS);
          gl.depthMask(true);
          gl.disable(gl.BLEND);
          gl.clearDepth(1);
          const background = pose.current.darkMode
            ? [23, 28, 36]
            : [237, 241, 245];
          gl.clearColor(
            background[0] / 255,
            background[1] / 255,
            background[2] / 255,
            1,
          );
          gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
          const angles = pose.current.anglesForModel(model);
          const links = linkTransforms(model, angles);
          const camera = tianjiCameraMatrix(w, h, view.current);
          gl.uniformMatrix4fv(uCamera, false, columnMajor(camera));
          for (const link of model.links)
            for (const visual of link.visuals) {
              const buffer = buffers[visual.mesh];
              gl.bindBuffer(gl.ARRAY_BUFFER, buffer.buffer);
              gl.enableVertexAttribArray(p);
              gl.vertexAttribPointer(p, 3, gl.FLOAT, false, 24, 0);
              gl.enableVertexAttribArray(n);
              gl.vertexAttribPointer(n, 3, gl.FLOAT, false, 24, 12);
              const scaling = identity();
              scaling[0] = visual.scale[0];
              scaling[5] = visual.scale[1];
              scaling[10] = visual.scale[2];
              gl.uniformMatrix4fv(
                uModel,
                false,
                columnMajor(
                  multiply(
                    links[link.name] ?? identity(),
                    multiply(transform(visual.xyz, visual.rpy), scaling),
                  ),
                ),
              );
              gl.uniform3fv(
                uColor,
                new Float32Array(
                  visual.color.slice(0,3).map((c) =>
                    pose.current.darkMode ? Math.min(1, c * 1.15) : c,
                  ),
                ),
              );
              gl.drawArrays(gl.TRIANGLES, 0, buffer.count);
            }
        };
        const resize = new ResizeObserver(() => draw.current?.());
        resize.observe(el);
        draw.current();
        setLoading(false);
        cleanup = () => {
          resize.disconnect();
          draw.current = null;
          Object.values(buffers).forEach((b) => gl.deleteBuffer(b.buffer));
          gl.deleteProgram(program);
          gl.deleteShader(vs);
          gl.deleteShader(fs);
        };
      })
      .catch((e) => {
        if (!abort.signal.aborted) {
          setError(e.message);
          setLoading(false);
        }
      });
    return () => {
      abort.abort();
      cleanup();
    };
  }, [modelUrl]);
  const zoom = (delta: number) => {
    view.current.zoom = Math.min(1.7, Math.max(0.6, view.current.zoom + delta));
    draw.current?.();
  };
  return (
    <div className="teleop-robot-preview tianji-robot-preview">
      <div className="teleop-robot-zoom">
        <button
          type="button"
          aria-label={"缩小"+title+"姿态"}
          onClick={() => zoom(-0.1)}
        >
          −
        </button>
        <button
          type="button"
          aria-label={"放大"+title+"姿态"}
          onClick={() => zoom(0.1)}
        >
          +
        </button>
        <button
          type="button"
          aria-label={"恢复"+title+"视角"}
          onClick={() => {
            view.current = { ...initialView };
            draw.current?.();
          }}
        >
          ↺
        </button>
      </div>
      {loading || error ? (
        <span className="tianji-preview-message">
          {error || '加载'+title+'装配模型…'}
        </span>
      ) : null}
      <canvas
        ref={canvas}
        className="teleop-robot-canvas"
        role="img"
        aria-label={title+" 3D 装配姿态"}
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
            e.preventDefault();
            view.current.yaw += e.key === 'ArrowLeft' ? -0.1 : 0.1;
            draw.current?.();
          }
        }}
        onPointerDown={(e) => {
          drag.current = { x: e.clientX, y: e.clientY };
          e.currentTarget.setPointerCapture(e.pointerId);
        }}
        onPointerMove={(e) => {
          if (!drag.current) return;
          view.current.yaw += (e.clientX - drag.current.x) * 0.008;
          view.current.pitch = Math.max(
            -0.6,
            Math.min(
              0.8,
              view.current.pitch + (e.clientY - drag.current.y) * 0.006,
            ),
          );
          drag.current = { x: e.clientX, y: e.clientY };
          draw.current?.();
        }}
        onPointerUp={() => {
          drag.current = null;
        }}
        onPointerCancel={() => {
          drag.current = null;
        }}
      />
    </div>
  );
}
