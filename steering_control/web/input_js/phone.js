"use strict";
const startButton = document.getElementById("start");
const stopButton = document.getElementById("stop");
const statusText = document.getElementById("status");
const partialText = document.getElementById("partial");
const finalText = document.getElementById("final");
const level = document.getElementById("level");
let context, media, source, processor, mute, sessionId;
let queue = [], pumping = null, sequence = 0, accepting = false, stopping = false;
let failure = null, flushAck = null, starting = false;
let lastSoundAt = 0;

function updateCaptureStatus() {
  if (!accepting || stopping || failure) return;
  const phase = window.dialogueUI && window.dialogueUI.captureStatus ? window.dialogueUI.captureStatus() : null;
  statusText.textContent = phase || (Date.now() - lastSoundAt < 900 ? "持续收音中：收到声音，正在识别" : "持续收音中：等待你说话");
}

async function request(path, body, binary = false) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(path, {
      method: "POST", cache: "no-store", signal: controller.signal,
      headers: {"Content-Type": binary ? "application/octet-stream" : "application/json"},
      body: binary ? body : JSON.stringify(body)
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `连接失败 (${response.status})`);
    return result;
  } finally { clearTimeout(timer); }
}

function render(result) {
  for (const event of result.events || []) {
    if (window.dialogueUI && window.dialogueUI.onTranscript) window.dialogueUI.onTranscript(event);
    if (event.type === "partial") partialText.textContent = event.text;
    if (event.type === "final") {
      if (event.text) finalText.textContent += (finalText.textContent ? "\n" : "") + event.text;
      partialText.textContent = "";
    }
  }
}

async function pump() {
  try {
    while (queue.length && !failure) {
      const chunk = queue.shift();
      const tag = chunk.tag ? `&tag=${encodeURIComponent(chunk.tag)}` : "";
      render(await request(`/api/audio?session=${encodeURIComponent(sessionId)}&sequence=${sequence}${tag}`, chunk.buffer, true));
      sequence++;
    }
  } catch (error) {
    failure = error;
    void stopCapture(error);
  }
}

function acceptChunk(buffer) {
  if (!accepting || failure) return;
  const values = new Float32Array(buffer);
  let energy = 0;
  for (const value of values) energy += value * value;
  level.value = Math.min(1, Math.sqrt(energy / Math.max(1, values.length)) * 6);
  if (Math.sqrt(energy / Math.max(1, values.length)) > 0.025) lastSoundAt = Date.now();
  updateCaptureStatus();
  if (queue.length >= 20) {
    failure = new Error("网络或识别速度跟不上，传声已停止。请检查 Wi-Fi 后重新开始。");
    void stopCapture(failure);
    return;
  }
  const filtered = window.dialogueUI && window.dialogueUI.filterAudio ? window.dialogueUI.filterAudio(values) : values;
  // Snapshot the caller's tag at capture time; queued audio cannot acquire a later request's identity.
  queue.push({buffer: filtered.buffer, tag: window.captureHooks && window.captureHooks.audioTag ? window.captureHooks.audioTag() : ""});
  if (!pumping) {
    pumping = pump().finally(() => { pumping = null; });
  }
}

async function stopCapture(error = null) {
  if (stopping) return;
  stopping = true;
  stopButton.disabled = true;
  statusText.textContent = "正在停止…";
  try {
    if (window.captureHooks && window.captureHooks.onStopping) await window.captureHooks.onStopping();
    if (processor && accepting) {
      await new Promise(resolve => {
        flushAck = resolve;
        setTimeout(resolve, 1000);
        processor.port.postMessage({type: "flush"});
      });
    }
    accepting = false;
    if (media) media.getTracks().forEach(track => track.stop());
    if (source) source.disconnect();
    if (processor) processor.disconnect();
    if (mute) mute.disconnect();
    if (context && context.state !== "closed") await context.close();
    if (pumping) await pumping;
    if (sessionId) render(await request("/api/stop", {session_id: sessionId, abort: Boolean(error || failure)}));
  } catch (stopError) {
    error = error || failure || stopError;
  } finally {
    accepting = false;
    if (media) media.getTracks().forEach(track => track.stop());
    context = media = source = processor = mute = sessionId = null;
    queue = []; pumping = null; flushAck = null; level.value = 0;
    stopping = false; starting = false;
    startButton.disabled = false;
    const problem = error || failure;
    statusText.textContent = problem ? `已停止：${problem.message || problem}` : "已停止，可以重新开始";
    if (window.captureHooks && window.captureHooks.onStopped) window.captureHooks.onStopped();
    if (problem) document.getElementById("setup").open = true;
  }
}

startButton.onclick = async () => {
  if (starting || stopping || sessionId) return;
  starting = true;
  startButton.disabled = true;
  failure = null; sequence = 0; queue = [];
  try {
    if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw new Error("浏览器尚未信任 HTTPS。请按下方说明安装本机 CA 证书，再重新打开页面。");
    }
    statusText.textContent = "正在请求麦克风权限…";
    context = new (window.AudioContext || window.webkitAudioContext)();
    await context.resume();
    media = await navigator.mediaDevices.getUserMedia({
      audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true},
      video: false
    });
    if (!context.audioWorklet) throw new Error("此浏览器不支持音频采集，请用最新版安卓 Chrome。");
    await context.audioWorklet.addModule("/capture.js");
    const dialogue = window.dialogueUI ? await window.dialogueUI.prepare() : null;
    const result = await request("/api/start", {sample_rate: context.sampleRate, dialogue_id: dialogue ? dialogue.dialogue_id : null});
    sessionId = result.session_id;
    partialText.textContent = "";
    source = context.createMediaStreamSource(media);
    processor = new AudioWorkletNode(context, "microphone-capture");
    processor.port.onmessage = event => {
      if (event.data instanceof ArrayBuffer) acceptChunk(event.data);
      else if (event.data.type === "flushed" && flushAck) flushAck();
    };
    mute = context.createGain(); mute.gain.value = 0;
    accepting = true;
    source.connect(processor); processor.connect(mute); mute.connect(context.destination);
    for (const track of media.getAudioTracks()) {
      track.onended = () => { if (accepting && !stopping) void stopCapture(new Error("手机麦克风已被系统关闭，请重新开始。")); };
    }
    stopButton.disabled = false; starting = false;
    lastSoundAt = 0;
    updateCaptureStatus();
    if (window.captureHooks && window.captureHooks.onStarted) await window.captureHooks.onStarted(sessionId);
  } catch (error) {
    if (error.name === "NotAllowedError") error = new Error("麦克风权限被拒绝；请在 Chrome 网站设置和安卓应用权限中允许麦克风。");
    await stopCapture(error);
  }
};
stopButton.onclick = () => { void stopCapture(); };
window.addEventListener("pagehide", () => {
  accepting = false;
  if (window.captureHooks && window.captureHooks.onStopped) window.captureHooks.onStopped();
  if (media) media.getTracks().forEach(track => track.stop());
  if (sessionId) navigator.sendBeacon("/api/stop", JSON.stringify({session_id: sessionId, abort: true}));
});
