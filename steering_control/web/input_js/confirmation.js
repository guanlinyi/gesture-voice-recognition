"use strict";
(() => {
  const byId = id => document.getElementById(id);
  let sid = null, context = null, source = null, resolvePlay = null, controller = null;
  let generation = 0, currentPrompt = null, listenId = "", commandToken = "", timer = null, stopped = true;
  const seen = new Set();
  let revision = -1;
  let message = "等待手机开始";
  let lastSubmittedId = null;
  let arming = null, answerLead = [];

  async function api(path, data) {
    const timeout = new AbortController();
    const clock = setTimeout(() => timeout.abort(), 15000);
    try {
      const response = await fetch(path, {method: "POST", cache: "no-store", signal: timeout.signal,
        headers: {"Content-Type": "application/json"}, body: JSON.stringify(data)});
      const result = await response.json();
      if (!response.ok) throw Error(result.error || "确认服务异常");
      return result;
    } finally { clearTimeout(clock); }
  }
  function stopPlayback() {
    generation++; listenId = ""; commandToken = ""; currentPrompt = null;
    arming = null; answerLead = [];
    if (controller) controller.abort();
    controller = null;
    if (source) { try { source.stop(); } catch (_) {} }
    source = null;
    if (resolvePlay) resolvePlay();
    resolvePlay = null;
  }
  function valid(stamp, session, request) {
    return !stopped && stamp === generation && sid === session && currentPrompt === request;
  }
  function display(snapshot) {
    if (snapshot.revision < revision) return;
    revision = snapshot.revision;
    message = snapshot.message;
    const arm = snapshot.arm_state;
    byId("arm-state").textContent = arm ? `当前状态：${arm.state}${arm.moving ? "（执行中）" : arm.state === "未知" ? "" : "（已完成）"}` : "当前状态：未知";
    byId("arm-rejection").textContent = snapshot.last_rejection && (!snapshot.last_action || snapshot.last_rejection.time >= snapshot.last_action.time) ? snapshot.last_rejection.text : "";
    if (window.conversationUI) window.conversationUI.update(snapshot.chat);
    byId("model").textContent = `模型：${snapshot.model}`;
    byId("time-hint").textContent = `播报结束后有 ${snapshot.answer_timeout || 8} 秒回答。否定、超时或没有明确确认均不执行；下一次重新做 OK。`;
    byId("phase").textContent = snapshot.voice.error ? `提示音准备失败：${snapshot.voice.error}` :
      (!snapshot.voice.ready ? `正在准备三种提示音，完成后再做手势。${message}` : message);
    const pending = snapshot.pending;
    const awaiting = pending && ["prompting", "listening", "dispatching"].includes(pending.state);
    const inference = snapshot.inference;
    byId("live-inference").textContent = inference ? `当前识别：${inference.caption}` : "等待实时推理";
    byId("live-state").textContent = awaiting ? "本次操作已锁定，确认结束后请重新做 OK 切换操作" :
      (inference && inference.prepared ? "已进入手势准备状态，也可直接语音下令" : "手势未进入准备状态，可直接语音下令");
    // Finished requests remain in history, but must not look like a current question.
    byId("question").textContent = awaiting ? (pending.prompt || "正在发送语音指令") : "等待手势或直接语音指令";
    const names = {"摆1字": "食指竖起", "手掌展开": "手掌张开"};
    const states = {confirmed: "已提交", rejected: "已拒绝", rejected_state: "当前状态不允许，未发送", cancelled: "已取消", expired: "已超时", dispatch_failed: "提交失败"};
    byId("request-label").textContent = pending ?
      `${awaiting ? "本次" : "上次"}：${names[pending.gesture] || pending.gesture} → ${pending.action}${awaiting ? "（待确认）" : `（${states[pending.state] || pending.state}）`}` : "尚无待确认操作";
    byId("cancel").disabled = !pending || !["prompting", "listening"].includes(pending.state);
    if (!pending || pending.state !== "listening") listenId = "";
    if (pending && !["prompting", "listening"].includes(pending.state) && currentPrompt === pending.id) stopPlayback();
    commandToken = !stopped && snapshot.input && snapshot.input.mode === "commands" ? snapshot.input.token : "";
    if (!stopped && snapshot.input && snapshot.input.mode === "confirmation" && pending && pending.id === currentPrompt) listenId = snapshot.input.token;
    if (snapshot.last_action) {
      const action = snapshot.last_action;
      if (action.request_id !== lastSubmittedId) {
        lastSubmittedId = action.request_id;
        if (window.conversationUI) window.conversationUI.stopReply();
      }
      byId("result").textContent = `${action.action}（${action.command} / 数字 ${action.id}），${action.source === "voice" ? "语音指令" : "确认回答"}：${action.confirmation}`;
    }
    if (pending && pending.state === "prompting" && !seen.has(pending.id)) {
      seen.add(pending.id);
      void playPrompt(pending);
    }
  }
  async function playPrompt(pending) {
    if (window.conversationUI) window.conversationUI.stopReply();
    stopPlayback();
    currentPrompt = pending.id;
    const stamp = generation, session = sid;
    controller = new AbortController();
    const fetchTimer = setTimeout(() => { if (valid(stamp, session, pending.id)) controller.abort(); }, 15000);
    try {
      const response = await fetch(`/api/confirmation/prompt?session=${encodeURIComponent(session)}&request=${encodeURIComponent(pending.id)}`,
        {cache: "no-store", signal: controller.signal});
      if (!response.ok) { const body = await response.json(); throw Error(body.error || "提示音获取失败"); }
      const bytes = await response.arrayBuffer();
      if (!valid(stamp, session, pending.id)) return;
      const audio = await context.decodeAudioData(bytes);
      if (!valid(stamp, session, pending.id)) return;
      const clip = context.createBufferSource();
      clip.buffer = audio; clip.connect(context.destination); source = clip;
      await new Promise(resolve => {
        resolvePlay = resolve;
        clip.onended = () => { if (source === clip) { source = null; resolvePlay = null; } resolve(); };
        clip.start();
      });
      if (!valid(stamp, session, pending.id)) return;
      // Retain speech that starts immediately after the question while awaiting the listen ACK.
      arming = pending.id; answerLead = [];
      const result = await api("/api/confirmation/listen", {session_id: session, request_id: pending.id,
        playback_token: pending.playback_token});
      if (!valid(stamp, session, pending.id)) return;
      arming = null;
      display(result);
      if (result.pending && result.pending.id === pending.id && result.pending.state === "listening") listenId = pending.id;
    } catch (error) {
      if (valid(stamp, session, pending.id)) {
        stopPlayback();
        message = `播报未完成，已取消：${error.message}`;
        byId("phase").textContent = message;
        try { await api("/api/confirmation/cancel", {session_id: session, request_id: pending.id}); } catch (_) {}
      }
    } finally { clearTimeout(fetchTimer); }
  }
  async function poll() {
    const session = sid;
    if (stopped || !session) return;
    try {
      const timeout = new AbortController();
      const clock = setTimeout(() => timeout.abort(), 10000);
      let response;
      try {
        response = await fetch(`/api/confirmation/status?session=${encodeURIComponent(session)}`, {cache: "no-store", signal: timeout.signal});
      } finally { clearTimeout(clock); }
      const result = await response.json();
      if (!response.ok) throw Error(result.error || "确认状态读取失败");
      if (stopped || session !== sid) return;
      display(result);
    } catch (error) {
      if (stopped || session !== sid) return;
      stopPlayback();
      message = `确认连接异常，已暂停：${error.message}。请结束收音后重新开始`;
      byId("phase").textContent = message;
      return; // fail closed: no new listening window after losing status
    }
    if (!stopped && session === sid) timer = setTimeout(poll, 200);
  }
  window.captureHooks = {
    audioTag: () => listenId || commandToken,
    onStarted: async session => {
      sid = session; stopped = false; seen.clear(); revision = -1; stopPlayback();
      display(await api("/api/confirmation/ready", {session_id: sid}));
      void poll();
    },
    onStopping: async () => {
      const session = sid;
      stopped = true;
      if (timer) clearTimeout(timer);
      stopPlayback(); // mute the final capture flush before it can be submitted
      if (window.conversationUI) window.conversationUI.stop();
      if (session) {
        try { await api("/api/confirmation/pause", {session_id: session}); } catch (_) {}
      }
    },
    onStopped: () => {
      stopped = true; sid = null;
      if (timer) clearTimeout(timer);
      stopPlayback();
      if (window.conversationUI) window.conversationUI.stop();
    }
  };
  window.dialogueUI = {
    prepare: async () => null,
    filterAudio: samples => {
      if (arming) {
        if (answerLead.length >= 20) {
          const request = arming, session = sid;
          stopPlayback();
          message = "确认连接较慢，本次未发送，请重新做 OK 和工作手势";
          void api("/api/confirmation/cancel", {session_id: session, request_id: request}).catch(() => {});
        } else answerLead.push(samples.slice());
        return new Float32Array(samples.length);
      }
      if (listenId) {
        if (answerLead.length) { answerLead.push(samples.slice()); return answerLead.shift(); }
        return samples;
      }
      return commandToken ? (window.conversationUI ? window.conversationUI.filterAudio(samples) : samples) : new Float32Array(samples.length);
    },
    captureStatus: () => window.conversationUI && window.conversationUI.captureStatus() || message,
    onTranscript: event => { if (window.conversationUI) window.conversationUI.onTranscript(event); }
  };
  const begin = byId("start").onclick;
  byId("start").onclick = async () => {
    try {
      if (!context) context = new (window.AudioContext || window.webkitAudioContext)();
      const unlockChat = window.conversationUI ? window.conversationUI.enable() : Promise.resolve();
      await context.resume(); // inside the start click, unlocking phone playback once
      await unlockChat;
      await begin();
    } catch (error) { byId("status").textContent = `无法开启语音确认：${error.message}`; }
  };
  byId("cancel").onclick = async () => {
    const request = currentPrompt, session = sid;
    stopPlayback();
    if (!request || !session) return;
    try { display(await api("/api/confirmation/cancel", {session_id: session, request_id: request})); }
    catch (error) { byId("phase").textContent = `取消请求失败：${error.message}，请结束收音`; }
  };
})();
