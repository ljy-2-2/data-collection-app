import {identity, multiply, transform, axisRotation, columnMajor, linkTransforms} from '../../lib/tianjiModel';
import type {TianjiModel, TianjiInstallation} from '../../lib/tianjiModel';

type Side = 'left' | 'right';
interface DirectionPreview {
  model: TianjiModel;
  installation: TianjiInstallation & {arm_channels: Record<Side, 'A' | 'B'>};
  traces: Record<Side, {label: string; delta: number[]; start: number[]; end: number[];
    frames: {q: number[]; tcp: number[]}[]; error_mm: number}[]>;
  initial: number[];
  display: number[];
  config_sha256: string;
  generated_at: string;
}

(async () => {
  const root = document.getElementById('tianji-six-directions')!;
  const query = <T = HTMLElement>(selector: string) => root.querySelector(selector)! as unknown as T;
  try {
    const bytes = Uint8Array.from(atob(query('[data-payload]').textContent!.trim()), character => character.charCodeAt(0));
    const data = await new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'))).json() as DirectionPreview;
    const sideSelect = query<HTMLSelectElement>('[data-side]');
    for (const option of Array.from(sideSelect.options)) {
      const selectedSide = option.value as Side;
      option.textContent = `${selectedSide === 'left' ? '左臂' : '右臂'} · ${data.installation.arm_channels[selectedSide]} 通道`;
    }
    root.dataset.configSha256 = data.config_sha256;
    root.dataset.installationId = data.installation.installation_id;
    query('[data-version]').textContent = `配置 ${data.config_sha256.slice(0, 8)} · ${data.generated_at.replace('T', ' ').replace('+00:00', ' UTC')}`;
    const canvas = query<HTMLCanvasElement>('[data-model]');
    const overlay = query<SVGSVGElement>('[data-overlay]');
    const hand = query<SVGSVGElement>('[data-hand]');
    const gl = canvas.getContext('webgl', {alpha: true, antialias: true});
    if (!gl) throw new Error('浏览器未启用 WebGL，无法显示机械臂模型。');
    const shader = (type: number, source: string) => {
      const result = gl.createShader(type)!;
      gl.shaderSource(result, source);
      gl.compileShader(result);
      if (!gl.getShaderParameter(result, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(result)!);
      return result;
    };
    const program = gl.createProgram()!;
    gl.attachShader(program, shader(gl.VERTEX_SHADER, 'attribute vec3 position;attribute vec3 normal;uniform mat4 model;uniform mat4 camera;varying vec3 norm;void main(){norm=mat3(model)*normal;gl_Position=camera*model*vec4(position,1.0);}'));
    gl.attachShader(program, shader(gl.FRAGMENT_SHADER, 'precision mediump float;varying vec3 norm;uniform vec3 color;void main(){vec3 unit=normalize(norm);if(!gl_FrontFacing)unit=-unit;float light=.42+.58*max(0.0,dot(unit,normalize(vec3(-.5,-.6,1.0))));gl_FragColor=vec4(color*light,1.0);}'));
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program)!);
    gl.useProgram(program);
    gl.enable(gl.DEPTH_TEST);
    const position = gl.getAttribLocation(program, 'position');
    const normal = gl.getAttribLocation(program, 'normal');
    gl.enableVertexAttribArray(position);
    gl.enableVertexAttribArray(normal);
    const modelUniform = gl.getUniformLocation(program, 'model');
    const cameraUniform = gl.getUniformLocation(program, 'camera');
    const colorUniform = gl.getUniformLocation(program, 'color');
    const buffers: Record<string, {buffer: WebGLBuffer; count: number}> = {};
    for (const [name, mesh] of Object.entries<any>(data.model.meshes)) {
      const values: number[] = [];
      for (const face of mesh.faces) {
        const vertices = face.map((index: number) => mesh.vertices[index]);
        const first = vertices[1].map((value: number, index: number) => value - vertices[0][index]);
        const second = vertices[2].map((value: number, index: number) => value - vertices[0][index]);
        const cross = [first[1]*second[2]-first[2]*second[1],first[2]*second[0]-first[0]*second[2],first[0]*second[1]-first[1]*second[0]];
        const length = Math.hypot(...cross) || 1;
        for (const vertex of vertices) values.push(...vertex, ...cross.map(value => value/length));
      }
      const buffer = gl.createBuffer()!;
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(values), gl.STATIC_DRAW);
      buffers[name] = {buffer, count: values.length / 6};
    }
    const zeroLinks = linkTransforms(data.model, {});
    const lower = [Infinity, Infinity, Infinity], upper = [-Infinity, -Infinity, -Infinity];
    for (const link of data.model.links) {
      for (const visual of link.visuals) {
        const matrix = multiply(zeroLinks[link.name], transform(visual.xyz, visual.rpy));
        for (const vertex of data.model.meshes[visual.mesh].vertices) {
          for (let axis = 0; axis < 3; axis++) {
            const coordinate = matrix[axis*4+3] + [0,1,2].reduce((sum, index) => sum + matrix[axis*4+index]*vertex[index]*visual.scale[index], 0);
            lower[axis] = Math.min(lower[axis], coordinate);
            upper[axis] = Math.max(upper[axis], coordinate);
          }
        }
      }
    }
    const sceneCenter = lower.map((value, index) => (value + upper[index])/2);
    const sceneHalfSize = upper.map((value, index) => (value-lower[index])/2);
    let side: Side = 'left';
    let direction = 0, progress = 0, animation = 0;
    const viewState = {yaw: .3, pitch: .85, zoom: 1};
    const viewSelect = query<HTMLSelectElement>('[data-view]');
    let drag: {pointerId: number; x: number; y: number} | null = null;
    const play = query<HTMLButtonElement>('[data-play]');
    const zero = query<HTMLButtonElement>('[data-zero]');
    const selected = () => data.traces[side][direction];
    const colors = () => {
      const probe = document.createElement('span');
      root.appendChild(probe);
      const resolve = (token: string) => {
        probe.style.color = `var(--${token})`;
        const context = document.createElement('canvas').getContext('2d')!;
        context.fillStyle = getComputedStyle(probe).color;
        context.fillRect(0, 0, 1, 1);
        return Array.from(context.getImageData(0, 0, 1, 1).data).slice(0, 3).map(value => value / 255);
      };
      const result = {body: resolve('muted-foreground'), active: resolve('blue'), stand: resolve('border')};
      probe.remove();
      return result;
    };
    let palette = colors();
    const svgLine = (start: number[], end: number[], color: string, arrow = false) => `<line x1="${start[0]}" y1="${start[1]}" x2="${end[0]}" y2="${end[1]}" stroke="var(--${color})" stroke-width="${arrow ? 3 : 1.5}" ${arrow ? 'marker-end="url(#six-arrow)"' : ''}/>`;
    const arrowDefinition = '<defs><marker id="six-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 Z" fill="var(--orange)"/></marker></defs>';
    function draw() {
      const bounds = canvas.getBoundingClientRect();
      const width = bounds.width, height = bounds.height, ratio = Math.min(devicePixelRatio || 1, 2);
      canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio);
      gl!.viewport(0, 0, canvas.width, canvas.height);
      gl!.clearColor(0, 0, 0, 0); gl!.clear(gl!.COLOR_BUFFER_BIT | gl!.DEPTH_BUFFER_BIT);
      const projection = identity();
      const upright = [0,-1,0,0, 0,0,1,0, -1,0,0,0, 0,0,0,1];
      const orientation = multiply(axisRotation([1,0,0], viewState.pitch), multiply(upright, axisRotation([0,0,1], viewState.yaw)));
      const horizontalExtent = sceneHalfSize.reduce((sum, value, axis) => sum + Math.abs(orientation[axis])*value, .16);
      const verticalExtent = sceneHalfSize.reduce((sum, value, axis) => sum + Math.abs(orientation[4+axis])*value, .16);
      const scale = .92 * Math.min(width / height / horizontalExtent, 1 / verticalExtent) * viewState.zoom;
      projection[0] = scale * height / width; projection[5] = scale; projection[10] = -.25;
      const camera = multiply(projection, multiply(orientation, transform(sceneCenter.map(value => -value),[0,0,0])));
      gl!.uniformMatrix4fv(cameraUniform, false, columnMajor(camera));
      const trace = selected(), frame = trace.frames[Math.round(progress * (trace.frames.length - 1))];
      const angles: Record<string, number> = {};
      for (const name of ['left', 'right'] as const) {
        const letter = data.installation.arm_models[name] === 'left' ? 'L' : 'R';
        const joints = name === side ? frame.q : data.initial;
        joints.forEach((value: number, index: number) => angles[`Joint${index+1}_${letter}`] = value * Math.PI / 180);
      }
      const links = linkTransforms(data.model, angles);
      const selectedLetter = data.installation.arm_models[side] === 'left' ? 'L' : 'R';
      for (const link of data.model.links) {
        for (const visual of link.visuals) {
          const mesh = buffers[visual.mesh];
          const scaling = identity();
          scaling[0] = visual.scale[0]; scaling[5] = visual.scale[1]; scaling[10] = visual.scale[2];
          const matrix = multiply(links[link.name], multiply(transform(visual.xyz, visual.rpy), scaling));
          gl!.uniformMatrix4fv(modelUniform, false, columnMajor(matrix));
          const active = link.name.endsWith(`_${selectedLetter}`) || link.name.startsWith(`gripper_${selectedLetter}`);
          gl!.uniform3fv(colorUniform, active ? palette.active : link.name === 'stand' ? palette.stand : palette.body);
          gl!.bindBuffer(gl!.ARRAY_BUFFER, mesh.buffer);
          gl!.vertexAttribPointer(position, 3, gl!.FLOAT, false, 24, 0);
          gl!.vertexAttribPointer(normal, 3, gl!.FLOAT, false, 24, 12);
          gl!.drawArrays(gl!.TRIANGLES, 0, mesh.count);
        }
      }
      const project = (point: number[]) => [
        (camera[0]*point[0]+camera[1]*point[1]+camera[2]*point[2]+camera[3]+1)*width/2,
        (1-camera[4]*point[0]-camera[5]*point[1]-camera[6]*point[2]-camera[7])*height/2,
      ];
      const start = project(trace.start), target = project(trace.start.map((value: number, index: number) => value + (trace.end[index]-value)*8));
      const actual = project(frame.tcp);
      overlay.setAttribute('viewBox', `0 0 ${width} ${height}`);
      let markup = arrowDefinition;
      const origin = [70, height - 55];
      const names = ['前 +X','左 +Y','上 +Z'];
      for (let axis = 0; axis < 3; axis++) {
        const endpoint = [origin[0]+camera[axis]*25,origin[1]-camera[4+axis]*25];
        if (Math.hypot(endpoint[0]-origin[0],endpoint[1]-origin[1]) < 4) continue;
        const offsets = [[-16,-4],[-8,16],[18,-6]][axis];
        markup += svgLine(origin, endpoint, 'muted-foreground') + `<text x="${endpoint[0]+offsets[0]}" y="${endpoint[1]+offsets[1]}" text-anchor="middle">${names[axis]}</text>`;
      }
      markup += `<polyline points="${trace.frames.map((value: any) => project(value.tcp).join(',')).join(' ')}" fill="none" stroke="var(--green)" stroke-width="3"/>`;
      markup += svgLine(start, target, 'orange', true);
      markup += `<circle cx="${actual[0]}" cy="${actual[1]}" r="5" fill="var(--foreground)" stroke="var(--background)" stroke-width="2"/>`;
      markup += `<text x="${Math.max(50,Math.min(width-70,start[0]))}" y="${Math.max(22,start[1]-16)}" text-anchor="middle">${side === 'left' ? '左臂' : '右臂'} TCP</text>`;
      if (Math.hypot(target[0]-start[0],target[1]-start[1]) < 5) markup += '<text x="12" y="24">此方向垂直屏幕，请切换斜视 / 侧视</text>';
      overlay.innerHTML = markup;
    }
    function update() {
      cancelAnimationFrame(animation); progress = 0;
      const trace = selected();
      root.querySelectorAll<HTMLButtonElement>('[data-direction]').forEach(button => button.setAttribute('aria-pressed', String(Number(button.dataset.direction) === direction)));
      const axis = trace.delta.findIndex((value: number) => Math.abs(value) > 1e-6);
      const output = [['前','后'],['左','右'],['上','下']][axis][trace.delta[axis] > 0 ? 0 : 1];
      query('[data-summary]').textContent = `手柄向${trace.label} → 模型目标向${output}`;
      query('[data-vector]').textContent = `世界 Δ[X,Y,Z] = [${trace.delta.map((value: number) => (value*1000).toFixed(0)).join(', ')}] mm`;
      query('[data-error]').textContent = `离线终点位置残差 ${trace.error_mm.toFixed(2)} mm`;
      const width = hand.getBoundingClientRect().width;
      hand.setAttribute('viewBox', `0 0 ${width} 145`);
      const center = [width/2,78];
      const vertical = direction > 3;
      const offsets = [[0,-48],[0,48],[-43,0],[43,0],[0,-48],[0,48]];
      const end = center.map((value,index) => value+offsets[direction][index]);
      hand.innerHTML = `<defs><marker id="six-hand-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto"><path d="M0 0 L10 5 L0 10 Z" fill="var(--blue)"/></marker></defs><text x="${width/2}" y="15" text-anchor="middle">手柄 · ${vertical?'侧视':'俯视'}</text><ellipse cx="${center[0]}" cy="67" rx="22" ry="18" fill="none" stroke="var(--muted-foreground)" stroke-width="3"/><rect x="${center[0]-10}" y="68" width="20" height="40" rx="9" fill="var(--muted)" stroke="var(--foreground)"/><circle cx="${center[0]}" cy="73" r="4" fill="var(--foreground)"/><line x1="${center[0]}" y1="${center[1]}" x2="${end[0]}" y2="${end[1]}" stroke="var(--blue)" stroke-width="3" marker-end="url(#six-hand-arrow)"/><text x="${width/2}" y="142" text-anchor="middle">向${trace.label} 20 mm</text>`;
      play.disabled = false; play.textContent = '从零位演示'; zero.disabled = false;
      query('[data-pose]').textContent = '双臂零位：J1–J7 均为 0°（非真机当前位置）';
      draw();
    }
    root.querySelectorAll<HTMLButtonElement>('[data-direction]').forEach(button => button.addEventListener('click', () => {direction = Number(button.dataset.direction); update();}));
    sideSelect.addEventListener('change', () => {side = sideSelect.value as Side; update();});
    viewSelect.addEventListener('change', () => {
      const preset = {oblique: [.3,.85], top: [0,Math.PI/2], side: [-Math.PI/2,0]}[viewSelect.value];
      if (!preset) return;
      [viewState.yaw, viewState.pitch] = preset;
      viewState.zoom = 1;
      draw();
    });
    canvas.addEventListener('pointerdown', event => {
      if (event.button !== 0 || drag) return;
      drag = {pointerId: event.pointerId, x: event.clientX, y: event.clientY};
      canvas.setPointerCapture(event.pointerId);
      canvas.dataset.dragging = 'true';
      event.preventDefault();
    });
    canvas.addEventListener('pointermove', event => {
      if (!drag || drag.pointerId !== event.pointerId) return;
      viewState.yaw += (event.clientX - drag.x) * .008;
      viewState.pitch = Math.max(-Math.PI/2, Math.min(Math.PI/2, viewState.pitch + (event.clientY - drag.y) * .008));
      drag.x = event.clientX; drag.y = event.clientY;
      viewSelect.value = 'custom';
      draw();
    });
    const endDrag = (event: PointerEvent) => {
      if (!drag || drag.pointerId !== event.pointerId) return;
      drag = null;
      delete canvas.dataset.dragging;
      if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
    };
    canvas.addEventListener('pointerup', endDrag);
    canvas.addEventListener('pointercancel', endDrag);
    canvas.addEventListener('lostpointercapture', endDrag);
    canvas.addEventListener('wheel', event => {
      event.preventDefault();
      const pixels = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? canvas.clientHeight : 1);
      viewState.zoom = Math.max(.5, Math.min(3, viewState.zoom * Math.exp(-pixels * .001)));
      draw();
    }, {passive: false});
    zero.addEventListener('click', update);
    play.addEventListener('click', () => {
      cancelAnimationFrame(animation);
      progress = 0;
      query('[data-pose]').textContent = '从双臂零位出发的离线 IK 演示（不是回零指令）';
      if (matchMedia('(prefers-reduced-motion: reduce)').matches) {progress = 1; draw(); play.textContent = '重新演示'; return;}
      const started = performance.now();
      play.disabled = true;
      const animate = (now: number) => {
        progress = Math.min((now-started)/3000,1); draw();
        if (progress < 1) animation = requestAnimationFrame(animate);
        else {play.disabled = false; play.textContent = '重新演示';}
      };
      animation = requestAnimationFrame(animate);
    });
    new ResizeObserver(() => {draw();}).observe(canvas.parentElement!);
    new MutationObserver(() => {palette = colors(); draw();}).observe(document.documentElement, {attributes: true});
    matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {palette = colors(); draw();});
    update();
    root.dataset.ready = 'true';
  } catch (error) {
    query('[data-error-message]').textContent = String(error);
  }
})();
