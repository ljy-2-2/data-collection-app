/**
 * Galaxis Monitor 国际化与主题切换
 * 全部资源均为本地,无任何外部依赖。
 */
"use strict";

const I18N = (() => {
  const dict = {
    zh: {
      stats: "采集统计",
      taskKind: "任务种类",
      episodes: "个 Episode",
      episodeElapsed: "Episode 计时",
      subtaskElapsed: "子任务计时",
      encoders: "磁编码器",
      vrStatus: "VR 状态",
      logs: "事件日志",
      position: "位置",
      rotation: "姿态",
      left: "左手",
      right: "右手",
      leftCtrl: "左手柄",
      rightCtrl: "右手柄",
      hmd: "头显",
      tracking: "追踪中",
      trackingLost: "追踪丢失",
      online: "在线",
      offline: "离线",
      recording: "录制中",
      notRecording: "未录制",
      standby: "待机",
      rosConnected: "ROS 已连接",
      rosDisconnected: "ROS 未连接",
      bridgeConnected: "桥接已连接",
      bridgeWaiting: "等待桥接连接…",
      bridgePolling: "WS 断开,轮询中…",
      bridgeNoRealtime: "实时通道不可用,轮询更新中…",
      mockData: "模拟数据",
      rosData: "ROS 数据",
      camera: "相机",
      storage: "存储",
      currentRound: "当前回合",
      unknownTask: "未确认",
      waitingStream: "等待视频流…",
      directFailed: "直连失败,切换代理…",
      proxyFailed: "代理失败,切换直连…",
      noSignal: "无信号",
      reconnecting: "重连中…",
      fullscreen: "全屏",
      taskKindTitle: "任务种类",
      modeMock: "MOCK",
      modeRos: "ROS",
      mockWarning: "模拟模式:数据为演示用,不代表真实采集状态",
      mockLog: "模拟模式启动",
      episodeDone: "Episode 完成,累计",
      bridgeStart: "桥接服务启动,模式:",
      startRecording: "开始录制",
      stopRecording: "停止录制",
      cancelRecording: "取消录制",
    },
    en: {
      stats: "Collection Stats",
      taskKind: "Task Category",
      episodes: " Episodes",
      episodeElapsed: "Episode Time",
      subtaskElapsed: "Subtask Time",
      encoders: "Encoders",
      vrStatus: "VR Status",
      logs: "Event Log",
      position: "Pos",
      rotation: "Quat",
      left: "Left",
      right: "Right",
      leftCtrl: "Left Ctrl",
      rightCtrl: "Right Ctrl",
      hmd: "Headset",
      tracking: "Tracking",
      trackingLost: "Tracking lost",
      online: "Online",
      offline: "Offline",
      recording: "Recording",
      notRecording: "Not Recording",
      standby: "Standby",
      rosConnected: "ROS Connected",
      rosDisconnected: "ROS Offline",
      bridgeConnected: "Bridge Connected",
      bridgeWaiting: "Waiting for bridge…",
      bridgePolling: "WS lost, polling…",
      bridgeNoRealtime: "Realtime channel down, polling…",
      mockData: "mock data",
      rosData: "ROS data",
      camera: "Cameras",
      storage: "Storage",
      currentRound: "Round",
      unknownTask: "Unknown",
      waitingStream: "Waiting for stream…",
      directFailed: "Direct failed, switching proxy…",
      proxyFailed: "Proxy failed, switching direct…",
      noSignal: "No signal",
      reconnecting: "Reconnecting…",
      fullscreen: "Fullscreen",
      taskKindTitle: "Task Category",
      modeMock: "MOCK",
      modeRos: "ROS",
      mockWarning: "Mock mode: demo data only, not real collection state",
      mockLog: "Mock mode started",
      episodeDone: "Episode done, total",
      bridgeStart: "Bridge started, mode:",
      startRecording: "Start",
      stopRecording: "Stop",
      cancelRecording: "Cancel",
    },
  };

  let lang = "zh";
  try {
    lang = localStorage.getItem("galaxis-lang") || "zh";
  } catch (e) { /* ignore */ }
  if (!dict[lang]) lang = "zh";

  function t(key, params) {
    let text = (dict[lang] && dict[lang][key]) || dict.zh[key] || key;
    if (params) {
      for (const [k, v] of Object.entries(params)) {
        text = text.replace(`{${k}}`, String(v));
      }
    }
    return text;
  }

  function applyStatic() {
    document.documentElement.setAttribute("data-lang", lang);
    document.querySelectorAll("[data-i18n]").forEach((el) => {
      el.textContent = t(el.getAttribute("data-i18n"));
    });
    const langBtn = document.getElementById("btn-lang");
    if (langBtn) langBtn.textContent = lang === "zh" ? "EN" : "中";
  }

  function setLang(next) {
    lang = dict[next] ? next : "zh";
    try {
      localStorage.setItem("galaxis-lang", lang);
    } catch (e) { /* ignore */ }
    applyStatic();
    if (typeof window.onLangChange === "function") window.onLangChange(lang);
  }

  function toggleLang() {
    setLang(lang === "zh" ? "en" : "zh");
  }

  /* ------------------------------ 主题 ------------------------------ */
  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    const btn = document.getElementById("btn-theme");
    if (btn) btn.textContent = theme === "light" ? "☾" : "☀";
  }

  function toggleTheme() {
    const current =
      document.documentElement.getAttribute("data-theme") === "light"
        ? "light"
        : "dark";
    const next = current === "light" ? "dark" : "light";
    try {
      localStorage.setItem("galaxis-theme", next);
    } catch (e) { /* ignore */ }
    applyTheme(next);
  }

  function init() {
    applyStatic();
    const theme =
      document.documentElement.getAttribute("data-theme") === "light"
        ? "light"
        : "dark";
    applyTheme(theme);
  }

  return { t, lang: () => lang, setLang, toggleLang, toggleTheme, init };
})();

window.I18N = I18N;
