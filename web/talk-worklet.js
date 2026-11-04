// Push-to-talk mic capture, on the audio thread (AudioWorklet). Downsamples
// whatever rate the device records at (44.1/48 kHz) to 8 kHz with a box
// average and posts 20 ms chunks of 16-bit PCM (160 samples) to the page,
// which sends them over the talk WebSocket. Smaller and steadier chunks than
// the old ScriptProcessor (deprecated, ran on the main thread).
class TalkCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 8000;   // `sampleRate` is a worklet global
    this.pos = 0; this.acc = 0; this.n = 0;
    this.out = new Int16Array(160); this.len = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;             // no mic data this block (yet)
    for (let i = 0; i < ch.length; i++) {
      this.acc += ch[i]; this.n++;
      if (++this.pos >= this.ratio) {
        this.pos -= this.ratio;
        let v = this.acc / this.n; this.acc = 0; this.n = 0;
        v = v < -1 ? -1 : v > 1 ? 1 : v;
        this.out[this.len++] = v < 0 ? v * 32768 : v * 32767;
        if (this.len === this.out.length) {
          this.port.postMessage(this.out.buffer, [this.out.buffer]);
          this.out = new Int16Array(160); this.len = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor('talk-capture', TalkCapture);
