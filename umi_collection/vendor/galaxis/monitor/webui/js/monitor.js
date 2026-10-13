/**
 * 采集监看页渲染逻辑
 */
"use strict";

(function () {
  // DOM 引用全部缓存:20 Hz 渲染下 getElementById 只查一次,后续字典命中
  const _elCache = {};
  const $ = (id) => _elCache[id] || (_elCache[id] = document.getElementById(id));
  const t = (key, params) => (window.I18N ? window.I18N.t(key, params) : key);

  // 渲染值缓存:25 Hz 推送下只有真正变化的内容才写 DOM,避免高频重排
  const renderedCache = {};
  function setText(el, key, text) {
    if (!el) return;
    if (renderedCache[key] === text) return;
    renderedCache[key] = text;
    el.textContent = text;
  }
  function setClass(el, key, value) {
    if (!el) return;
    if (renderedCache[key] === value) return;
    renderedCache[key] = value;
    el.className = value;
  }

  const api = new StudioApi(window.location.origin);
  const grid = new CameraGrid("camera-grid", api.baseUrl);
  window.__cameraGrid = grid; // 供 streams.js 的全局断流扫描使用

  let renderedLogs = 0;
  const encoderLastValue = { left: 0, right: 0 };
  const vrLastPose = {};
  // 监看刷新预算:VR 位姿 10 Hz;磁编码器数字仅作参考,限 5 Hz 省出主线程
  let encoderValueIntervalMs = 200; // 5 Hz
  let vrPoseIntervalMs = 100;       // 10 Hz

  function applyPreviewBudget(camCount) {
    // 当前各相机数量档位共用;如需再为 3 相机降频,在此按 camCount 分支
    encoderValueIntervalMs = 200;
    vrPoseIntervalMs = 100;
  }

  /* ------------------------------ 工具 ------------------------------ */
  function fmtTime(sec) {
    if (sec == null || isNaN(sec) || sec < 0) return "00:00";
    sec = Math.floor(sec);
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return h > 0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
  }

  function fmtVec(values, digits = 3) {
    if (!Array.isArray(values) || !values.length) return "--";
    return values.map((v) => Number(v).toFixed(digits)).join(", ");
  }

  function fmtHz(hz) {
    if (hz == null || isNaN(hz) || hz <= 0) return "-- Hz";
    return `${hz.toFixed(1)} Hz`;
  }

  /* --------------------------- 顶部状态 chip --------------------------- */
  function renderChips(state) {
    const modeChip = $("chip-mode");
    const isMock = state.mode === "mock";
    setText(modeChip, "chip-mode", isMock ? t("modeMock") : t("modeRos"));
    setClass(modeChip, "chip-mode-cls", isMock ? "chip warn" : "chip");
    if (renderedCache["chip-mode-color"] !== isMock) {
      renderedCache["chip-mode-color"] = isMock;
      modeChip.style.color = isMock ? "var(--warn)" : "var(--accent)";
    }

    const rosChip = $("chip-ros");
    const rosOk = !!state.ros_connected;
    setClass(rosChip, "chip-ros-cls", "chip " + (rosOk ? "ok" : ""));
    setText(rosChip.querySelector("span"), "chip-ros", rosOk
      ? t("rosConnected")
      : t("rosDisconnected"));

    const recChip = $("chip-record");
    const recording = !!state.recording;
    setClass(recChip, "chip-record-cls", "chip " + (recording ? "rec" : ""));
    setText(recChip.querySelector("span"), "chip-record", recording
      ? t("recording")
      : t("notRecording"));
  }

  /* --------------------------- 采集统计卡片 --------------------------- */
  function renderEpisode(state) {
    const ep = state.episode || {};
    setText($("episode-count"), "ep-count", String(ep.completed_total ?? 0));

    // 任务种类
    const task = state.task || {};
    const taskName = task.name || t("unknownTask");
    setText($("task-kind"), "ep-task", taskName);

    // 当前回合命名:<任务名> #<序号>
    setText($("episode-label"), "ep-label", ep.label
      ? `${t("currentRound")}: ${ep.label}`
      : `${t("currentRound")}: —`);

    const badge = $("badge-status");
    let badgeText;
    let badgeCls;
    if (state.recording) {
      badgeText = t("recording");
      badgeCls = "badge rec";
    } else if (state.ros_connected) {
      badgeText = t("standby");
      badgeCls = "badge ok";
    } else {
      badgeText = t("offline");
      badgeCls = "badge";
    }
    setText(badge, "ep-badge", badgeText);
    setClass(badge, "ep-badge-cls", badgeCls);

    setText($("episode-elapsed"), "ep-elapsed", fmtTime(ep.elapsed_sec));
    setText($("subtask-elapsed"), "ep-subtask", fmtTime(ep.subtask_elapsed_sec));

    const btnStart = $("btn-start");
    const btnStop = $("btn-stop");
    const btnCancel = $("btn-cancel");
    btnStart.disabled = state.recording;
    btnStop.disabled = !state.recording;
    btnCancel.disabled = !state.recording;
  }

  /* --------------------------- 磁编码器卡片 --------------------------- */
  // DOM 引用缓存:20 Hz 渲染下避免每帧重复 querySelector
  const encEls = {};
  function getEncEls(side) {
    if (encEls[side]) return encEls[side];
    const row = document.querySelector(`.encoder-row[data-side="${side}"]`);
    if (!row) return null;
    encEls[side] = {
      row,
      idEl: row.querySelector('[data-field="id"]'),
      hzBadge: row.querySelector('[data-field="hz"]'),
      valueEl: row.querySelector('[data-field="value"]'),
    };
    return encEls[side];
  }

  function renderEncoders(state) {
    const encoders = state.encoders || {};
    for (const side of ["left", "right"]) {
      const els = getEncEls(side);
      if (!els) continue;
      const { row, idEl, hzBadge, valueEl } = els;
      const info = encoders[side] || {};
      row.hidden = info.active === false; // 未启用该手则不显示
      if (row.hidden) continue;

      const online = !!info.online;
      row.classList.toggle("offline", !online);
      setText(idEl, `enc-${side}-id`,
        info.device_id ? `ID ${info.device_id}` : "--");
      const hzText = online ? fmtHz(info.rate_hz) : t("offline");
      setText(hzBadge, `enc-${side}-hz`, hzText);
      setClass(hzBadge, `enc-${side}-hz-cls`, "hz-badge" + (online && info.rate_hz > 0 ? " live" : ""));

      if (online) {
        const deg = ((info.position || 0) * 180) / Math.PI;
        // 数字按监看预算节流,录制数据不受影响
        const now = performance.now();
        if (now - encoderLastValue[side] >= encoderValueIntervalMs) {
          encoderLastValue[side] = now;
          setText(valueEl, `enc-${side}-value`, `${deg.toFixed(1)}°`);
        }
      } else {
        encoderLastValue[side] = 0;
        setText(valueEl, `enc-${side}-value`, "--");
      }
    }
  }

  /* ------------------------------- VR 卡片 ------------------------------- */
  // DOM 引用缓存:20 Hz 渲染下避免每帧重复 querySelector
  const vrEls = {};
  function getVrEls(device) {
    if (vrEls[device]) return vrEls[device];
    const row = document.querySelector(`.vr-cell[data-device="${device}"]`);
    if (!row) return null;
    vrEls[device] = {
      row,
      stateEl: row.querySelector(".vr-state"),
      hzBadge: row.querySelector('[data-field="hz"]'),
      posEl: row.querySelector('[data-field="pos"]'),
      rotEl: row.querySelector('[data-field="rot"]'),
    };
    return vrEls[device];
  }

  function renderVr(state) {
    const vr = state.vr || {};
    const labels = { hmd: "头显", left: "左手柄", right: "右手柄" };
    for (const device of Object.keys(labels)) {
      const els = getVrEls(device);
      if (!els) continue;
      const { row, stateEl, hzBadge, posEl, rotEl } = els;
      const info = vr[device] || {};
      row.classList.toggle("offline", !info.online);
      if (info.online) {
        // 位姿全零(无有效历史时)显示 "--",避免误导成真实位姿
        const pos = Array.isArray(info.position) ? info.position : [];
        const rot = Array.isArray(info.rotation) ? info.rotation : [];
        const posLost = pos.length === 0 || pos.every((v) => Math.abs(Number(v)) < 1e-9);
        const rotLost = rot.length === 0 || rot.every((v) => Math.abs(Number(v)) < 1e-9);
        // PICO 端协议未回传 tracked 标志,以位姿有效性作为追踪状态判定
        const tracking = !posLost && !rotLost;
        setText(stateEl, `vr-${device}-state`, tracking ? t("tracking") : t("trackingLost"));
        setClass(stateEl, `vr-${device}-state-cls`, "vr-state " + (tracking ? "tracking" : "online"));
        setText(hzBadge, `vr-${device}-hz`, fmtHz(info.rate_hz));
        setClass(hzBadge, `vr-${device}-hz-cls`, "hz-badge" + (info.rate_hz > 0 ? " live" : ""));
        // 位姿数字按监看预算节流,把 CPU 让给视频解码
        const now = performance.now();
        if (now - (vrLastPose[device] || 0) >= vrPoseIntervalMs) {
          vrLastPose[device] = now;
          setText(posEl, `vr-${device}-pos`,
            !posLost ? `[${fmtVec(pos)}] m` : "--");
          setText(rotEl, `vr-${device}-rot`,
            !rotLost ? `[${fmtVec(rot)}]` : "--");
        }
      } else {
        vrLastPose[device] = 0; // 离线立即显示
        setText(stateEl, `vr-${device}-state`, t("offline"));
        setClass(stateEl, `vr-${device}-state-cls`, "vr-state");
        setText(hzBadge, `vr-${device}-hz`, "-- Hz");
        setClass(hzBadge, `vr-${device}-hz-cls`, "hz-badge");
        setText(posEl, `vr-${device}-pos`, "--");
        setText(rotEl, `vr-${device}-rot`, "--");
      }
    }
  }

  /* ------------------------------- 日志 ------------------------------- */
  function renderLogs(state) {
    const logs = state.logs || [];
    if (logs.length === renderedLogs) return;
    const scroll = $("log-scroll");
    const frag = document.createDocumentFragment();
    for (let i = renderedLogs; i < logs.length; i++) {
      const entry = logs[i];
      const line = document.createElement("div");
      line.className = "log-line " + (entry.level || "info");
      const t = new Date((entry.t || 0) * 1000);
      const ts = `${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}:${String(t.getSeconds()).padStart(2, "0")}`;
      line.innerHTML = `<span class="t">${ts}</span><span class="m"></span>`;
      line.querySelector(".m").textContent = entry.msg || "";
      frag.appendChild(line);
    }
    scroll.appendChild(frag);
    renderedLogs = logs.length;
    scroll.scrollTop = scroll.scrollHeight;
  }

  /* ---------------------------- 底部信息栏 ---------------------------- */
  function renderFooter(state, connectionOk) {
    if (!connectionOk) {
      $("footer-state").textContent = t("bridgeWaiting");
    } else {
      $("footer-state").textContent = api.connected
        ? `${t("bridgeConnected")} · ${state.mode === "mock" ? t("mockData") : t("rosData")}`
        : t("bridgeNoRealtime");
    }
    const cams = state.cameras || [];
    $("footer-video").textContent = cams.length
      ? `${t("camera")}: ${cams.map((c) => c.label).join(" / ")}`
      : `${t("camera")}: --`;

    // 存储只占底部角落一行小字
    const storage = state.storage_gb || {};
    $("storage-tiny").textContent = storage.total
      ? `${t("storage")} ${storage.used} GB / ${storage.total} GB`
      : `${t("storage")} --`;
  }

  /* ------------------------------ 主渲染 ------------------------------ */
  function render(state) {
    renderChips(state);
    renderEpisode(state);
    renderEncoders(state);
    renderVr(state);
    renderLogs(state);
    renderFooter(state, true);
    grid.sync(state.cameras);
    applyPreviewBudget(grid.streams.length);
  }

  function renderOffline() {
    const rosChip = $("chip-ros");
    rosChip.className = "chip";
    rosChip.querySelector("span").textContent = t("rosDisconnected");
    if (api.state) {
      $("footer-state").textContent = t("bridgePolling");
    } else {
      $("footer-state").textContent = t("bridgeWaiting");
    }
  }

  /* ---------------------------- 控制按钮 ---------------------------- */
  function setupControls(health) {
    const actions = $("control-actions");
    actions.hidden = !health.enable_control;

    const run = async (action) => {
      try {
        await api.record(action);
      } catch (err) {
        appendLocalLog("err", `控制失败: ${err.message}`);
      }
    };
    $("btn-start").addEventListener("click", () => run("start"));
    $("btn-stop").addEventListener("click", () => run("stop"));
    $("btn-cancel").addEventListener("click", () => run("cancel"));
  }

  function appendLocalLog(level, msg) {
    const scroll = $("log-scroll");
    const line = document.createElement("div");
    line.className = "log-line " + level;
    line.innerHTML = `<span class="t">本地</span><span class="m"></span>`;
    line.querySelector(".m").textContent = msg;
    scroll.appendChild(line);
    scroll.scrollTop = scroll.scrollHeight;
  }

  /* ------------------------------ 启动 ------------------------------ */
  function boot() {
    if (window.I18N) window.I18N.init();
    const themeBtn = $("btn-theme");
    if (themeBtn && window.I18N) {
      themeBtn.addEventListener("click", () => window.I18N.toggleTheme());
    }
    const langBtn = $("btn-lang");
    if (langBtn && window.I18N) {
      langBtn.addEventListener("click", () => window.I18N.toggleLang());
    }
    // 语言切换后重渲染动态内容与相机卡片覆盖层
    window.onLangChange = () => {
      if (api.state) render(api.state);
      grid.streams.forEach((s) => s.refreshOverlay());
    };

    // 语言切换后重渲染动态内容与相机卡片覆盖层
    window.onLangChange = () => {
      if (api.state) render(api.state);
      grid.streams.forEach((s) => s.refreshOverlay());
    };

    $("log-clear").addEventListener("click", () => {
      $("log-scroll").innerHTML = "";
      renderedLogs = (api.state && api.state.logs) ? api.state.logs.length : 0;
    });

    let pendingState = null;
    let rafScheduled = false;
    api.onState = (state) => {
      // 后端可推 20 Hz,按动画帧合并渲染,避免高频全量 DOM 更新造成卡顿
      pendingState = state;
      if (rafScheduled) return;
      rafScheduled = true;
      requestAnimationFrame(() => {
        rafScheduled = false;
        const latest = pendingState;
        pendingState = null;
        if (latest) render(latest);
      });
    };
    api.onConnection = (ok) => {
      if (!ok) renderOffline();
      else appendLocalLog("ok", t("bridgeConnected"));
    };

    api.health()
      .then((health) => {
        setupControls(health);
        if (health.mode === "mock") {
          appendLocalLog("warn", t("mockWarning"));
        }
      })
      .catch(() => appendLocalLog("err", t("bridgeWaiting")));

    api.fetchState().catch(() => { /* 初始拉取失败由轮询兜底 */ });
    api.connect();
  }

  document.addEventListener("DOMContentLoaded", boot);
})();

