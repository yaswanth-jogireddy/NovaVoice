// Runs at a requested 16 kHz AudioContext rate. Emit 40 ms PCM frames.
class MicrophoneCapture extends AudioWorkletProcessor {
  constructor(options = {}) {
    super(); this.samples = new Int16Array(640); this.floats = new Float32Array(640); this.offset = 0;
    this.energy = 0; this.loudFrames = 0; this.quietFrames = 0; this.speaking = false;
    this.voicedRun = 0; this.bestVoicedRun = 0;
    this.noiseFloor = 0.003;
    const config = options.processorOptions || {};
    this.minFrames = Math.ceil((config.min_speech_ms ?? 240) / 40);
    this.releaseFrames = Math.ceil((config.release_ms ?? 200) / 40);
    this.minRms = config.min_rms ?? 0.018;
    this.noiseRatio = config.noise_ratio ?? 3.5;
    // Knocks, bottle thumps and clinks are loud but not voiced. Require a
    // continuous voiced stretch (a vowel) before treating sound as speech.
    this.minVoicedFrames = Math.ceil((config.min_voiced_ms ?? 120) / 40);
  }
  // Voiced speech repeats at a human pitch (70-400 Hz) and has few zero
  // crossings; hiss, clicks and metallic ringing do not. Mean removal ignores DC offset.
  voiced(x) {
    let mean = 0; for (const v of x) mean += v; mean /= x.length;
    let crossings = 0, previous = x[0] - mean;
    for (let i = 1; i < x.length; i++) { const v = x[i] - mean; if ((v >= 0) !== (previous >= 0)) crossings++; previous = v; }
    if (crossings / x.length > .25) return false;
    let best = 0;
    for (let lag = 40; lag <= 228; lag += 2) {
      let xy = 0, xx = 0, yy = 0;
      for (let i = 0; i + lag < x.length; i++) { const a = x[i] - mean, b = x[i + lag] - mean; xy += a * b; xx += a * a; yy += b * b; }
      if (xx && yy) best = Math.max(best, xy / Math.sqrt(xx * yy));
    }
    return best > .5;
  }
  process(inputs) {
    const input = inputs[0]?.[0];
    if (input) for (const value of input) {
      const clamped = Math.max(-1, Math.min(1, value));
      this.energy += clamped * clamped;
      this.floats[this.offset] = clamped;
      this.samples[this.offset++] = clamped < 0 ? clamped * 32768 : clamped * 32767;
      if (this.offset === 640) {
        const rms = Math.sqrt(this.energy / 640);
        const loud = rms > Math.max(this.minRms, this.noiseFloor * this.noiseRatio);
        if (!loud && !this.speaking) this.noiseFloor = this.noiseFloor * .98 + rms * .02;
        this.quietFrames = loud ? 0 : this.quietFrames + 1;
        // Count voiced frames, allowing one short gap between syllables.
        // Separated clicks cannot accumulate into an interruption.
        if (loud) this.loudFrames++;
        else if (this.quietFrames >= 2) { this.loudFrames = 0; this.bestVoicedRun = 0; }
        this.voicedRun = loud && !this.speaking && this.voiced(this.floats) ? this.voicedRun + 1 : 0;
        this.bestVoicedRun = Math.max(this.bestVoicedRun, this.voicedRun);
        if (!this.speaking && this.loudFrames >= this.minFrames && this.bestVoicedRun >= this.minVoicedFrames) {
          this.speaking = true; this.port.postMessage({type:'speech_start'});
        }
        if (this.speaking && this.quietFrames >= this.releaseFrames) {
          // quiet_ms: how long ago the caller actually stopped (the release wait).
          this.speaking = false; this.port.postMessage({type:'speech_end', quiet_ms:this.quietFrames * 40});
        }
        this.port.postMessage(this.samples.buffer, [this.samples.buffer]);
        this.samples = new Int16Array(640); this.offset = 0; this.energy = 0;
      }
    }
    return true;
  }
}
registerProcessor('microphone-capture', MicrophoneCapture);
