class MicrophoneCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Float32Array(Math.round(sampleRate / 10));
    this.offset = 0;
    this.stopped = false;
    this.port.onmessage = event => {
      if (event.data.type === "flush") {
        this.stopped = true;
        if (this.offset) {
          const tail = this.buffer.slice(0, this.offset);
          this.port.postMessage(tail.buffer, [tail.buffer]);
        }
        this.offset = 0;
        this.port.postMessage({type: "flushed"});
      }
    };
  }
  process(inputs) {
    const channels = inputs[0];
    if (this.stopped || !channels || !channels.length) return true;
    for (let index = 0; index < channels[0].length; index++) {
      let value = 0;
      for (const channel of channels) value += channel[index];
      this.buffer[this.offset++] = value / channels.length;
      if (this.offset === this.buffer.length) {
        const chunk = this.buffer;
        this.buffer = new Float32Array(chunk.length);
        this.offset = 0;
        this.port.postMessage(chunk.buffer, [chunk.buffer]);
      }
    }
    return true;
  }
}
registerProcessor("microphone-capture", MicrophoneCapture);
