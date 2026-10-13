/**
 * MJPEG 相机流管理
 * 每个相机卡片:FPS 统计、断流重连、代理失败后试直连、直连失败回代理。
 */
"use strict";

class CameraStream {
  constructor(container, camera, fallbackBase, preview) {
    this.camera = camera;                       // {key,label,topic,direct_url,proxy_url}
    this.fallbackBase = fallbackBase;           // 代理前缀(同源时可直接用 proxy_url)
    this.preview = preview || { fps: 30, width: 0 }; // 预览帧率与缩放宽度
    // 本机监看默认直连 web_video_server 的 C++ 预览流：保留源
    // 相机 30 FPS，同时避免 Python 代理复制整帧带来的 CPU 竞争。
    this.usingProxy = !this.camera.direct_url && Boolean(this.camera.proxy_url);
    this._triedDirect = Boolean(this.camera.direct_url);
    this.failed = 0;
    this.running = false;
    this._timer = null;
    this._retryDelay = 1000;
    this._fpsFrames = 0;
    this._fpsWindow = 0;
    this._lastOnload = 0;

    this.el = this._build(container);
    this.img = this.el.querySelector("img");
    this.fpsBadge = this.el.querySelector(".fps-badge");
    this.overlay = this.el.querySelector(".cam-overlay");
    this.overlayText = this.el.querySelector(".overlay-text");
    this.overlayKey = "waitingStream";
    this._wireEvents();
    this._applyOverlay();
  }

  _build(container) {
    const card = document.createElement("article");
    card.className = "cam-card";
    card.dataset.key = this.camera.key;
    card.innerHTML = `
      <header class="cam-head">
        <span class="cam-title"></span>
        <span class="cam-topic"></span>
        <span class="fps-badge">0.0 FPS</span>
        <button class="icon-btn fullscreen-btn" data-i18n-title="fullscreen" title="全屏">⛶</button>
      </header>
      <div class="cam-body">
        <img alt="" style="display:none" />
        <div class="cam-overlay">
          <div class="scan"></div>
          <small class="overlay-text">等待视频流…</small>
        </div>
      </div>`;
    card.querySelector(".cam-title").textContent = this.camera.label;
    card.querySelector(".cam-topic").textContent = this.camera.topic;
    container.appendChild(card);
    return card;
  }

  _applyOverlay() {
    this.overlayText.textContent = window.I18N
      ? window.I18N.t(this.overlayKey)
      : this.overlayKey;
    const btn = this.el.querySelector(".fullscreen-btn");
    if (btn && window.I18N) {
      btn.title = window.I18N.t("fullscreen");
    }
  }

  _setOverlay(key) {
    this.overlayKey = key;
    this._applyOverlay();
  }

  refreshOverlay() {
    this._applyOverlay();
  }

  updateCamera(camera) {
    const changed =
      camera.direct_url !== this.camera.direct_url ||
      camera.proxy_url !== this.camera.proxy_url ||
      camera.topic !== this.camera.topic;
    this.camera = camera;
    if (!changed || !this.running) return;
    clearTimeout(this._timer);
    this.usingProxy = !this.camera.direct_url && Boolean(this.camera.proxy_url);
    this._triedDirect = Boolean(this.camera.direct_url);
    this.failed = 0;
    this._retryDelay = 1000;
    const url = this._url();
    if (url) this.img.src = url;
  }

  _wireEvents() {
    const card = this.el;
    const btn = card.querySelector(".fullscreen-btn");
    btn.addEventListener("click", () => {
      if (card.classList.contains("fullscreen")) {
        card.classList.remove("fullscreen");
        btn.textContent = "⛶";
      } else {
        card.classList.add("fullscreen");
        btn.textContent = "⤢";
      }
    });

    this.img.onload = () => {
      this.img.style.display = "block";
      this.overlay.style.display = "none";
      this._tickFps();
      this.failed = 0;
      this._retryDelay = 1000;
    };
    this.img.onerror = () => this._handleError();
  }

  _url() {
    let base;
    if (this.usingProxy) {
      base = this._proxyUrl() + `&fps=${this.preview.fps}`;
      // width=0 时直接透传原始 JPEG，不引入解码/重编码负担。
      if (this.preview.width > 0) {
        base += `&width=${this.preview.width}&quality=60`;
      }
    } else {
      base = this.camera.direct_url;
    }
    if (!base) return "";
    return `${base}${base.includes("?") ? "&" : "?"}_=${Date.now()}`;
  }

  _proxyUrl() {
    if (this.camera.proxy_url && this.camera.proxy_url.startsWith("/")) {
      return this.fallbackBase.replace(/\/$/, "") + this.camera.proxy_url;
    }
    return this.camera.proxy_url;
  }

  _showStall() {
    this.img.style.display = "none";
    this.fpsBadge.textContent = "0.0 FPS";
    this.fpsBadge.className = "fps-badge";
    this.overlay.style.display = "flex";
  }

  _handleError() {
    this.failed += 1;
    this._showStall();
    // 状态机:默认代理(低带宽缩图)。代理不稳时试一次直连;
    // 直连也不稳(30 fps 原图更易被波动打断)则回代理并锚定,避免来回抖动。
    if (this.usingProxy && !this._triedDirect && this.failed >= 2 && this.camera.direct_url) {
      this.usingProxy = false;
      this._triedDirect = true;
      this.failed = 0;
      this._setOverlay("proxyFailed");
      this._reload();
      return;
    }
    if (!this.usingProxy && this.failed >= 3 && this.camera.proxy_url) {
      this.usingProxy = true;
      this.failed = 0;
      this._setOverlay("reconnecting");
      this._reload();
      return;
    }
    if (this.failed >= 3) {
      this._setOverlay("noSignal");
    } else {
      this._setOverlay("reconnecting");
      this._reload();
    }
  }

  _reload() {
    if (!this.running) return;
    clearTimeout(this._timer);
    // 指数退避,避免录制时偶发断帧引发重连风暴(旧连接未及时回收会越采越卡)
    const delay = this._retryDelay || 1000;
    this._retryDelay = Math.min(delay * 2, 15000);
    this._timer = setTimeout(() => {
      if (this.running) {
        // 时间戳参数已保证是新请求;不要先清空 src,否则会触发一次
        // error 事件,污染失败计数并导致过早切换直连(偶发卡顿主因)
        this.img.src = this._url();
      }
    }, delay);
  }

  _tickFps() {
    const now = performance.now();
    if (this._fpsWindow === 0) {
      this._fpsWindow = now;
      this._fpsFrames = 0;
    }
    this._fpsFrames += 1;
    const elapsed = (now - this._fpsWindow) / 1000;
    if (elapsed >= 1.0) {
      const fps = this._fpsFrames / elapsed;
      this.fpsBadge.textContent = `${fps.toFixed(1)} FPS`;
      this.fpsBadge.className = "fps-badge " + (fps < 2 ? "stall" : "live");
      this._fpsWindow = now;
      this._fpsFrames = 0;
    }
    // 15 秒完全无帧才视为断流并重连(相机 30 fps,15 秒无帧才可能是真断线);
    // 断流检查由全局 3 秒一次的扫描执行,不在每帧创建/销毁定时器,
    // 避免 3 路 × 30 fps 的定时器风暴拖慢页面
    this._lastOnload = performance.now();
  }

  start() {
    this.running = true;
    this.usingProxy = !this.camera.direct_url && Boolean(this.camera.proxy_url);
    this._triedDirect = Boolean(this.camera.direct_url);
    this.failed = 0;
    this._retryDelay = 1000;
    this._lastOnload = performance.now();
    const url = this._url();
    if (url) this.img.src = url;
  }

  stop() {
    this.running = false;
    if (this._timer) clearTimeout(this._timer);
    // 停用时先摘掉事件处理器,清空 src 不会触发重连计数
    this.img.onload = null;
    this.img.onerror = null;
    this.img.src = "";
  }
}

class CameraGrid {
  constructor(containerId, fallbackBase) {
    this.container = document.getElementById(containerId);
    this.fallbackBase = fallbackBase;
    this.streams = [];
  }

  /** 全局低频断流扫描(每 3 秒),发现 15 秒无帧的流才触发重连 */
  checkStreams() {
    const now = performance.now();
    for (const stream of this.streams) {
      if (stream.running && now - stream._lastOnload > 15000) {
        stream._handleError();
      }
    }
  }

  /** 按 /api/state 的 cameras 字段初始化(幂等) */
  sync(cameras) {
    if (!cameras || !cameras.length) return;
    // 预览只是监看，不能改变相机或录包速率。所有窗口都保持
    // 30 FPS，并且不做二次 JPEG 缩放/编码，避免网页代理反而抢占 CPU。
    const three = cameras.length >= 3;
    this.container.classList.toggle("layout-3", three);
    const known = new Map(this.streams.map((stream) => [stream.camera.key, stream]));
    for (const cam of cameras) {
      const existing = known.get(cam.key);
      if (existing) {
        existing.updateCamera(cam);
        continue;
      }
      const preview = { fps: 30, width: 0 };
      const stream = new CameraStream(this.container, cam, this.fallbackBase, preview);
      stream.start();
      this.streams.push(stream);
    }
  }
}

window.CameraStream = CameraStream;

// 断流检测:每 3 秒全局扫描一次(不再每帧创建定时器)
setInterval(() => {
  if (window.__cameraGrid) window.__cameraGrid.checkStreams();
}, 3000);
window.CameraGrid = CameraGrid;
