"use strict";
(() => {
  const panel = document.getElementById("dialogue");
  const status = document.getElementById("dialogue-status");
  const turns = document.getElementById("dialogue-turns");
  const input = document.getElementById("dialogue-text");
  const send = document.getElementById("dialogue-send");
  let dialogueId = null, creating = null, revision = -1, timer = null, hidden = false;
  let latestTurns = [], voiceReady = false;
  const voiceToggle = document.getElementById("voice-toggle");
  const voiceStatus = document.getElementById("voice-status");
  const interrupt = document.getElementById("voice-interrupt");
  const player = window.OrinReply ? new window.OrinReply.ReplyPlayer(voiceStatus, () => interrupt.checked, () => dialogueId) : null;
  if (player) player.onEnabledChanged = enabled => { voiceToggle.textContent = enabled ? "关闭语音回答" : "开启语音回答"; };
  const labels = {queued: "等待回答", thinking: "正在思考", answering: "正在回答", done: "已回答", error: "回答失败", cancelled: "已停止"};
  panel.hidden = false;
  document.getElementById("start").textContent = "开始持续对话";

  async function call(path, body) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(path, {method: body ? "POST" : "GET", cache: "no-store", signal: controller.signal,
        headers: {"Content-Type": "application/json"}, body: body ? JSON.stringify(body) : undefined});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "对话连接失败");
      return result;
    } finally { clearTimeout(timeout); }
  }

  function display(snapshot) {
    if (snapshot.revision <= revision) return;
    revision = snapshot.revision;
    latestTurns = snapshot.turns;
    voiceReady = Boolean(snapshot.voice_ready);
    if (player) player.consume(snapshot.turns);
    turns.replaceChildren();
    for (const turn of snapshot.turns) {
      const item = document.createElement("div");
      item.style.marginBottom = "18px";
      const question = document.createElement("div");
      question.textContent = `你：${turn.user}`;
      question.style.color = "#8fe0cb";
      const answer = document.createElement("div");
      answer.textContent = `助手：${turn.assistant || "…"}`;
      const state = document.createElement("small");
      state.textContent = turn.error || labels[turn.state] || turn.state;
      item.append(question, answer, state);
      turns.append(item);
    }
    const active = snapshot.turns.filter(turn => ["queued", "thinking", "answering"].includes(turn.state));
    status.textContent = active.length ? `${labels[active[0].state]}${active.length > 1 ? `，另有 ${active.length - 1} 个问题等待` : ""}` :
      snapshot.turns.length ? "可以继续追问" : "说完一句后自动回答，也可以输入文字";
  }

  async function poll() {
    timer = null;
    if (!dialogueId || hidden) return;
    try { display(await call(`/api/dialogue/status?dialogue=${encodeURIComponent(dialogueId)}`)); }
    catch (error) { status.textContent = `对话连接异常：${error.message}；如电脑已重启，请刷新页面`; }
    finally { if (!hidden) timer = setTimeout(poll, 200); }
  }

  async function prepare() {
    if (dialogueId) return {dialogue_id: dialogueId};
    if (!creating) {
      creating = call("/api/dialogue/start", {}).then(snapshot => {
        dialogueId = snapshot.dialogue_id;
        revision = -1;
        display(snapshot);
        if (!timer && !hidden) timer = setTimeout(poll, 200);
        return snapshot;
      }).finally(() => { creating = null; });
    }
    return creating;
  }

  async function action(path) {
    if (player) player.stop();
    try {
      const snapshot = await prepare();
      display(await call(path, {dialogue_id: snapshot.dialogue_id}));
    } catch (error) { status.textContent = error.message; }
  }

  send.onclick = async () => {
    const text = input.value.trim();
    if (!text) return;
    send.disabled = true;
    try {
      const snapshot = await prepare();
      const interrupted = player ? player.stop() : [];
      if (interrupted.length) await call("/api/dialogue/cancel", {dialogue_id: snapshot.dialogue_id, turn_ids: interrupted});
      display(await call("/api/dialogue/text", {dialogue_id: snapshot.dialogue_id, text}));
      input.value = "";
    } catch (error) { status.textContent = error.message; }
    finally { send.disabled = false; }
  };
  document.getElementById("dialogue-cancel").onclick = () => action("/api/dialogue/cancel");
  document.getElementById("dialogue-reset").onclick = () => action("/api/dialogue/reset");
  voiceToggle.onclick = async () => {
    if (!player) { voiceStatus.textContent = "语音组件未加载，请刷新页面"; return; }
    if (player.enabled) { player.disable(); voiceToggle.textContent = "开启语音回答"; return; }
    try {
      // AudioContext is resumed inside the user gesture, before network awaits.
      await player.enable(latestTurns);
      await prepare();
      if (!voiceReady) { player.disable(); throw new Error("请先在电脑运行 spoken_reply/prepare_voice.py 准备离线声音，再重启服务"); }
      voiceToggle.textContent = "关闭语音回答";
    } catch (error) { voiceStatus.textContent = error.message; }
  };
  window.dialogueUI = {prepare,
    captureStatus: () => {
      if (player && player.busy) return interrupt.checked ? "持续对话中：准备/朗读回答，仍在收音，可打断" : "持续对话中：准备/朗读回答，结束后自动继续听你说";
      if (latestTurns.some(turn => ["queued", "thinking", "answering"].includes(turn.state))) return "持续对话中：正在生成回答，仍在收音";
      return null;
    },
    filterAudio: samples => player ? player.filterAudio(samples) : samples,
    onTranscript: event => {
      if (player && interrupt.checked && player.busy && event.type === "partial" && event.text.trim()) {
        const ids = player.stop();
        if (ids.length && dialogueId) void call("/api/dialogue/cancel", {dialogue_id: dialogueId, turn_ids: ids}).then(display).catch(error => { voiceStatus.textContent = error.message; });
      }
    }};
  window.addEventListener("pagehide", () => { hidden = true; if (timer) clearTimeout(timer); if (player) player.disable(); });
})();
