// Voice in the page: the microphone (recorded here, transcribed by XENIO's own STT) and speech playback (XENIO's own
// TTS). No speech service or key is touched from the browser. Levels come from real audio through Web Audio
// analysers; they feed the orb and nothing else.

export type VoiceErrorKind = "denied" | "missing" | "unsupported" | "silent" | "failed";

export class VoiceError extends Error {
  readonly kind: VoiceErrorKind;
  constructor(kind: VoiceErrorKind, message: string) {
    super(message);
    this.name = "VoiceError";
    this.kind = kind;
  }
}

const TARGET_RATE = 16_000;
let shared: AudioContext | null = null;

/** One audio context for the page, created or resumed inside a click (browsers require a user gesture). */
export async function unlockAudio(): Promise<AudioContext | null> {
  const Ctor = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!Ctor) return null;
  shared ??= new Ctor();
  if (shared.state === "suspended") {
    try { await shared.resume(); } catch { /* resumed by the next gesture */ }
  }
  return shared;
}

function rms(analyser: AnalyserNode, buffer: Float32Array<ArrayBuffer>): number {
  analyser.getFloatTimeDomainData(buffer);
  let sum = 0;
  for (const v of buffer) sum += v * v;
  return Math.min(1, Math.sqrt(sum / buffer.length) * 9);
}

export function encodeWav(chunks: Float32Array[], inputRate: number): Blob {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const ratio = inputRate / TARGET_RATE;
  const length = Math.floor(total / ratio);
  const pcm = new Int16Array(length);
  let read = 0;
  const flat = new Float32Array(total);
  for (const c of chunks) { flat.set(c, read); read += c.length; }
  for (let i = 0; i < length; i++) { // average each window: a simple, alias-reducing downsample
    const start = Math.floor(i * ratio), end = Math.min(total, Math.floor((i + 1) * ratio));
    let acc = 0;
    for (let j = start; j < end; j++) acc += flat[j]!;
    const v = Math.max(-1, Math.min(1, acc / Math.max(1, end - start)));
    pcm[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
  }
  const header = new DataView(new ArrayBuffer(44));
  const text = (o: number, s: string) => { for (let i = 0; i < s.length; i++) header.setUint8(o + i, s.charCodeAt(i)); };
  text(0, "RIFF"); header.setUint32(4, 36 + pcm.byteLength, true); text(8, "WAVE"); text(12, "fmt ");
  header.setUint32(16, 16, true); header.setUint16(20, 1, true); header.setUint16(22, 1, true);
  header.setUint32(24, TARGET_RATE, true); header.setUint32(28, TARGET_RATE * 2, true);
  header.setUint16(32, 2, true); header.setUint16(34, 16, true); text(36, "data"); header.setUint32(40, pcm.byteLength, true);
  return new Blob([header.buffer, pcm.buffer], { type: "audio/wav" });
}

export interface Recording {
  /** 0..1 loudness of the live microphone */
  level: () => number;
  /** stop now and hand over what was said */
  stop: () => void;
  /** stop now and throw it away */
  cancel: () => void;
  /** the WAV, or null when cancelled */
  done: Promise<Blob | null>;
}

export interface RecordOptions {
  maxSeconds?: number;
  silenceSeconds?: number; // after speech, this much quiet ends the recording
  noSpeechSeconds?: number; // nothing heard at all: give up
}

export async function startRecording(options: RecordOptions = {}): Promise<Recording> {
  const { maxSeconds = 30, silenceSeconds = 1.3, noSpeechSeconds = 8 } = options;
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new VoiceError("unsupported", "This browser cannot use a microphone here. Type your request instead.");
  }
  const context = await unlockAudio();
  if (!context) throw new VoiceError("unsupported", "This browser has no audio support. Type your request instead.");
  let stream: MediaStream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 } });
  } catch (error) {
    const name = error instanceof DOMException ? error.name : "";
    if (name === "NotAllowedError" || name === "SecurityError") {
      throw new VoiceError("denied", "Microphone access is blocked. Allow it for this page in your browser's site settings " +
        "(and for the browser in System Settings > Privacy & Security > Microphone), or type instead.");
    }
    if (name === "NotFoundError" || name === "OverconstrainedError") {
      throw new VoiceError("missing", "No microphone was found. Connect one, or type your request instead.");
    }
    throw new VoiceError("failed", "The microphone could not be started. Type your request instead.");
  }
  const source = context.createMediaStreamSource(stream);
  const analyser = context.createAnalyser();
  analyser.fftSize = 1024;
  const levelBuffer = new Float32Array(analyser.fftSize);
  // ScriptProcessor is deprecated but works everywhere (Safari included); it only copies samples out
  const processor = context.createScriptProcessor(4096, 1, 1);
  const mute = context.createGain();
  mute.gain.value = 0;
  source.connect(analyser);
  source.connect(processor);
  processor.connect(mute).connect(context.destination);

  const chunks: Float32Array[] = [];
  let finished = false;
  let heardAt = 0;
  let quietSince = 0;
  const started = performance.now();
  let resolve!: (blob: Blob | null) => void;
  let reject!: (error: VoiceError) => void;
  const done = new Promise<Blob | null>((res, rej) => { resolve = res; reject = rej; });

  const finish = (keep: boolean, reason?: VoiceError) => {
    if (finished) return;
    finished = true;
    processor.onaudioprocess = null;
    source.disconnect();
    processor.disconnect();
    mute.disconnect();
    stream.getTracks().forEach((t) => t.stop());
    if (reason) reject(reason);
    else resolve(keep && chunks.length ? encodeWav(chunks, context.sampleRate) : null);
  };

  processor.onaudioprocess = (e) => {
    chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
    const now = performance.now();
    const loud = rms(analyser, levelBuffer) > 0.05;
    if (loud) { heardAt ||= now; quietSince = 0; } else if (heardAt) { quietSince ||= now; }
    if (heardAt && quietSince && now - quietSince > silenceSeconds * 1000) finish(true);
    else if (!heardAt && now - started > noSpeechSeconds * 1000) {
      finish(false, new VoiceError("silent", "Nothing was heard. Check the microphone, or type your request."));
    } else if (now - started > maxSeconds * 1000) finish(true);
  };

  return { level: () => (finished ? 0 : rms(analyser, levelBuffer)), stop: () => finish(true), cancel: () => finish(false), done };
}

/** Speech playback: SPEAKING is reported only between the audio really starting and really ending. */
export class Speaker {
  private source: AudioBufferSourceNode | null = null;
  private analyser: AnalyserNode | null = null;
  private buffer = new Float32Array(1024);

  level = (): number => (this.analyser ? rms(this.analyser, this.buffer) / 1.8 : 0);

  async play(wav: ArrayBuffer, events: { onStart: () => void; onEnd: () => void }): Promise<void> {
    this.stop();
    const context = await unlockAudio();
    if (!context) throw new VoiceError("unsupported", "This browser cannot play XENIO's voice; the answer is shown as text.");
    let decoded: AudioBuffer;
    try {
      decoded = await context.decodeAudioData(wav.slice(0));
    } catch {
      throw new VoiceError("failed", "XENIO's voice could not be played; the answer is shown as text.");
    }
    if (context.state !== "running") {
      throw new VoiceError("failed", "The browser blocked audio until you click somewhere on the page; the answer is shown as text.");
    }
    const source = context.createBufferSource();
    const analyser = context.createAnalyser();
    analyser.fftSize = 1024;
    source.buffer = decoded;
    source.connect(analyser).connect(context.destination);
    this.source = source;
    this.analyser = analyser;
    await new Promise<void>((resolve) => {
      source.onended = () => {
        if (this.source === source) { this.source = null; this.analyser = null; }
        events.onEnd();
        resolve();
      };
      source.start();
      events.onStart();
    });
  }

  stop(): void {
    const playing = this.source;
    this.source = null;
    this.analyser = null;
    try { playing?.stop(); } catch { /* already stopped */ }
  }
}
