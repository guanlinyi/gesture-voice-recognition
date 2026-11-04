"use strict";
(() => {
  const byId = id => document.getElementById(id);
  let dialogueId = null, revision = -1, latestTurns = [];
  const status = byId("chat-status"), voice = byId("chat-voice-status");
  const interrupt = byId("chat-interrupt"), toggle = byId("chat-voice");
  const player = new window.OrinReply.ReplyPlayer(voice, () => interrupt.checked, () => dialogueId);
  player.onEnabledChanged = enabled => { toggle.textContent = enabled ? "关闭聊天朗读" : "开启聊天朗读"; };
  const labels = {queued: "等待回答", thinking: "正在思考", answering: "正在回答", done: "已回答", error: "回答失败", cancelled: "已停止"};

  async function cancel(ids) {
    if (!ids.length || !dialogueId) return;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch("/api/dialogue/cancel", {method: "POST", signal: controller.signal,
        headers: {"Content-Type": "application/json"}, body: JSON.stringify({dialogue_id: dialogueId, turn_ids: ids})});
      if (!response.ok) throw Error("停止聊天请求失败");
    } catch (error) { status.textContent = error.message; }
    finally { clearTimeout(timer); }
  }
  function stopReply() {
    const ids = [...new Set([...player.stop(), ...latestTurns.filter(turn => ["queued", "thinking", "answering"].includes(turn.state)).map(turn => turn.id)])];
    void cancel(ids);
  }
  function update(chat) {
    if (!chat || !chat.dialogue) return;
    const snapshot = chat.dialogue;
    if (dialogueId !== snapshot.dialogue_id) {
      player.stop(); dialogueId = snapshot.dialogue_id; revision = -1; latestTurns = [];
    }
    if (snapshot.revision >= revision) {
      revision = snapshot.revision; latestTurns = snapshot.turns;
      byId("chat-turns").textContent = latestTurns.map(turn =>
        `你：${turn.user}\n助手：${turn.assistant || "…"}\n${turn.error || labels[turn.state] || turn.state}`).join("\n\n");
      player.consume(latestTurns);
    }
    const active = latestTurns.find(turn => ["queued", "thinking", "answering"].includes(turn.state));
    status.textContent = chat.phase === "understanding" || chat.phase === "error" ? chat.message :
      active ? labels[active.state] : "可以继续聊天，也可以发出方向盘指令";
  }
  async function enable() { await player.enable(latestTurns); }
  toggle.onclick = () => {
    if (player.enabled) { player.disable(); return; }
    void enable().catch(error => { voice.textContent = error.message; });
  };
  byId("chat-stop").onclick = stopReply;
  window.conversationUI = {enable, update, stopReply,
    stop: () => { stopReply(); player.disable(); },
    filterAudio: samples => player.filterAudio(samples),
    onTranscript: event => {
      if (interrupt.checked && event.type === "partial" && event.text.trim() &&
          (player.busy || latestTurns.some(turn => ["queued", "thinking", "answering"].includes(turn.state)))) stopReply();
    },
    captureStatus: () => player.busy ? (interrupt.checked ? "正在朗读聊天回答，仍在收音，可打断" : "正在朗读聊天回答，读完自动继续听") : null
  };
})();
