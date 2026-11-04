"use strict";
(() => {
  // Offsets count Unicode code points, matching Python slices (including emoji).
  function segments(text, offset, finished, limit = 32) {
    const chars = Array.from(text);
    const result = [];
    while (offset < chars.length) {
      const chunkLimit = offset === 0 ? Math.min(limit, 24) : limit;
      const endLimit = Math.min(offset + chunkLimit, chars.length);
      let end = -1;
      for (let i = offset; i < endLimit; i++) {
        if (/[。！？!?\n；;]/u.test(chars[i])) { end = i + 1; break; }
        if (i - offset >= 7 && /[，：]/u.test(chars[i])) { end = i + 1; break; }
      }
      if (end < 0) {
        if (endLimit === chars.length && !finished && chars.length - offset < chunkLimit) break;
        end = endLimit;
      }
      if (chars.slice(offset, end).join("").trim()) result.push({start: offset, end});
      offset = end;
    }
    return {segments: result, offset};
  }

  class ReplyPlayer {
    constructor(status, autoInterrupt, getDialogue) {
      this.status = status; this.autoInterrupt = autoInterrupt; this.getDialogue = getDialogue;
      this.context = null; this.enabled = false; this.queue = []; this.offsets = new Map();
      this.blocked = new Set(); this.epoch = 0; this.processing = false;
      this.source = null; this.controllers = new Set(); this.active = null; this.resolvePlayback = null;
      this.prefetched = null;
      this.cooldownUntil = 0;
      this.onEnabledChanged = () => {};
    }
    async enable(turns) {
      if (!this.context) this.context = new (window.AudioContext || window.webkitAudioContext)();
      await this.context.resume();
      this.stop();
      this.enabled = true;
      this.onEnabledChanged(true);
      for (const turn of turns) this.offsets.set(turn.id, Array.from(turn.assistant).length);
      this.status.textContent = "语音回答已开启，将朗读接下来的回答";
    }
    stop() {
      const hadAudio = Boolean(this.source);
      const ids = [...new Set([...(this.active ? [this.active.id] : []), ...(this.prefetched ? [this.prefetched.job.id] : []), ...this.queue.map(job => job.id)])];
      ids.forEach(id => this.blocked.add(id));
      this.epoch++;
      this.queue = []; this.active = null; this.processing = false;
      this.prefetched = null;
      for (const controller of this.controllers) controller.abort();
      this.controllers.clear();
      if (this.source) { try { this.source.stop(); } catch (_) {} }
      this.source = null;
      if (this.resolvePlayback) this.resolvePlayback();
      this.resolvePlayback = null;
      if (hadAudio) this.cooldownUntil = Date.now() + 400;
      this.status.textContent = this.enabled ? "朗读已停止" : "语音回答未开启";
      return ids;
    }
    disable() { this.stop(); this.enabled = false; this.onEnabledChanged(false); this.status.textContent = "语音回答已关闭"; }
    get busy() { return this.processing || this.queue.length > 0 || Boolean(this.source) || Boolean(this.prefetched); }
    filterAudio(samples) {
      if (!this.autoInterrupt() && ((this.enabled && this.busy) || Date.now() < this.cooldownUntil)) {
        return new Float32Array(samples.length);
      }
      return samples;
    }
    consume(turns) {
      const liveIds = new Set(turns.map(turn => turn.id));
      for (const id of this.offsets.keys()) if (!liveIds.has(id)) { this.offsets.delete(id); this.blocked.delete(id); }
      for (const turn of turns) {
        if (!this.enabled || this.blocked.has(turn.id) || ["cancelled", "error"].includes(turn.state)) {
          if (this.active && this.active.id === turn.id && ["cancelled", "error"].includes(turn.state)) this.stop();
          this.offsets.set(turn.id, Array.from(turn.assistant).length);
          this.queue = this.queue.filter(job => job.id !== turn.id);
          continue;
        }
        const next = segments(turn.assistant, this.offsets.get(turn.id) || 0, turn.state === "done");
        this.offsets.set(turn.id, next.offset);
        for (const segment of next.segments) this.queue.push({id: turn.id, ...segment});
      }
      if (this.enabled && this.processing && this.source) this.startPrefetch(this.epoch);
      if (this.enabled && this.queue.length && !this.processing) void this.drain();
    }
    async play(audio, epoch) {
      if (epoch !== this.epoch) return;
      const source = this.context.createBufferSource();
      source.buffer = audio;
      source.connect(this.context.destination);
      this.source = source;
      this.status.textContent = this.autoInterrupt() ? "正在朗读，可对着麦克风打断（建议戴耳机）" : "正在朗读，暂不识别输入；可点“停止回答”";
      await new Promise(resolve => {
        this.resolvePlayback = resolve;
        source.onended = () => {
          if (this.source === source) { this.source = null; this.resolvePlayback = null; this.cooldownUntil = Date.now() + 400; }
          resolve();
        };
        source.start();
      });
    }
    async fetchAudio(job, epoch) {
      const controller = new AbortController();
      this.controllers.add(controller);
      const timeout = setTimeout(() => controller.abort(), 60000);
      try {
        const response = await fetch("/api/dialogue/speech", {method: "POST", signal: controller.signal,
          headers: {"Content-Type": "application/json"}, body: JSON.stringify({dialogue_id: this.getDialogue(), turn_id: job.id, start: job.start, end: job.end})});
        if (!response.ok) { const error = await response.json(); throw new Error(error.error || "语音合成失败"); }
        const bytes = await response.arrayBuffer();
        if (epoch !== this.epoch) return null;
        const audio = await this.context.decodeAudioData(bytes);
        return epoch === this.epoch ? audio : null;
      } finally {
        clearTimeout(timeout);
        this.controllers.delete(controller);
      }
    }
    startPrefetch(epoch) {
      if (this.prefetched || epoch !== this.epoch || !this.enabled) return;
      while (this.queue.length) {
        const job = this.queue.shift();
        if (this.blocked.has(job.id)) continue;
        // Capture failures until this clip's turn, avoiding unhandled rejections.
        this.prefetched = {job, promise: this.fetchAudio(job, epoch).then(audio => ({audio}), error => ({error}))};
        break;
      }
    }
    async drain() {
      const epoch = this.epoch;
      this.processing = true;
      try {
        while ((this.prefetched || this.queue.length) && this.enabled && epoch === this.epoch) {
          const pending = this.prefetched;
          this.prefetched = null;
          const job = pending ? pending.job : this.queue.shift();
          if (this.blocked.has(job.id)) continue;
          this.active = job;
          this.status.textContent = "正在合成语音…";
          const result = pending ? await pending.promise : {audio: await this.fetchAudio(job, epoch)};
          if (result.error) throw result.error;
          if (!result.audio || epoch !== this.epoch) return;
          const playing = this.play(result.audio, epoch);
          this.startPrefetch(epoch);
          await playing;
          if (epoch === this.epoch) this.active = null;
        }
      } catch (error) {
        if (epoch === this.epoch) {
          this.stop();
          this.enabled = false;
          this.onEnabledChanged(false);
          this.status.textContent = `朗读已关闭：${error.message}。文字回答仍可使用`;
        }
      } finally {
        if (epoch === this.epoch) {
          this.processing = false; this.active = null;
          if (this.enabled) this.status.textContent = "本段朗读完成，等待下一段回答";
        }
      }
    }
  }
  window.OrinReply = {segments, ReplyPlayer};
})();
