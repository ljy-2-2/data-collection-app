/**
 * Galaxis Studio 桥接服务客户端
 * 优先 WebSocket 推送,失败自动降级为轮询。
 */
"use strict";

class StudioApi {
  constructor(baseUrl) {
    this.baseUrl = (baseUrl || window.location.origin).replace(/\/$/, "");
    this.state = null;
    this.onState = null;          // (state) => void
    this.onConnection = null;     // (connected: boolean) => void
    this.ws = null;
    this._reconnectDelay = 1000;
    this._pollTimer = null;
    this._wsOk = false;
    this.connected = false;
  }

  async connect() {
    this._openWs();
  }

  async health() {
    const res = await fetch(`${this.baseUrl}/api/health`);
    if (!res.ok) throw new Error(`health ${res.status}`);
    return res.json();
  }

  async fetchState() {
    const res = await fetch(`${this.baseUrl}/api/state`);
    if (!res.ok) throw new Error(`state ${res.status}`);
    this.state = await res.json();
    if (this.onState) this.onState(this.state);
    return this.state;
  }

  _openWs() {
    const wsUrl = this.baseUrl.replace(/^http/, "ws") + "/api/ws";
    try {
      this.ws = new WebSocket(wsUrl);
    } catch (e) {
      this._startPolling();
      return;
    }
    this.ws.onopen = () => {
      this._wsOk = true;
      this.connected = true;
      this._stopPolling();
      // WS 就绪但尚未收到任何状态时,立即拉一次快照补齐首屏
      if (!this.state) this.fetchState().catch(() => {});
      if (this.onConnection) this.onConnection(true);
    };
    this.ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg && msg.type === "patch" && msg.fields) {
          // 增量补丁:只合并变化字段,未收到全量快照前忽略
          if (!this.state) return;
          Object.assign(this.state, msg.fields);
        } else {
          this.state = msg;
        }
        if (this.onState) this.onState(this.state);
      } catch (e) { /* 忽略坏帧 */ }
    };
    this.ws.onclose = () => {
      this._wsOk = false;
      this.connected = false;
      if (this.onConnection) this.onConnection(false);
      // 降级轮询同时保持 WS 重连尝试
      this._startPolling();
      setTimeout(() => {
        if (!this._wsOk) this._openWs();
      }, this._reconnectDelay);
      this._reconnectDelay = Math.min(this._reconnectDelay * 1.5, 10000);
    };
    this.ws.onerror = () => { try { this.ws.close(); } catch (e) { /* noop */ } };
  }

  _startPolling() {
    if (this._pollTimer) return;
    this._pollTimer = setInterval(() => {
      this.fetchState().catch(() => { /* 桥接离线,等待恢复 */ });
    }, 1500);
  }

  _stopPolling() {
    if (this._pollTimer) { clearInterval(this._pollTimer); this._pollTimer = null; }
    this._reconnectDelay = 1000;
  }

  /** 录制控制(桥接开启控制接口后才可用) */
  async record(action) {
    const res = await fetch(`${this.baseUrl}/api/record/${action}`, { method: "POST" });
    if (!res.ok) throw new Error(`record ${action} ${res.status}`);
    return res.json();
  }
}

window.StudioApi = StudioApi;
