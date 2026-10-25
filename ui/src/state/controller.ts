// The page's side of a conversation: sending, stopping, the microphone and XENIO's voice. Everything that decides
// anything happens in XENIO. When XENIO's talk engine runs (the Mac's own microphone and speaker, "Hey XENIO"), the
// microphone button asks it to listen and XENIO speaks for itself; otherwise the page records and plays the audio.

import { ApiError, type XenioApi } from "../api/client";
import type { XenioEvent } from "../api/types";
import { Speaker, startRecording, unlockAudio, VoiceError, type Recording } from "../voice/audio";
import { Store } from "./store";

export class AppController {
  readonly speaker = new Speaker();
  /** how the microphone is recorded (replaceable in tests) */
  recorder: typeof startRecording = startRecording;
  private recording: Recording | null = null;
  private unsubscribe: (() => void) | null = null;
  private voiceTurns = new Set<string>();
  private quietTurns = new Set<string>();
  private speakAbort: AbortController | null = null;
  private loading: Promise<void> | null = null;
  private engineLevelValue = 0;
  /** the floating orb's page: never plays audio itself (the main page or XENIO's engine does) */
  silent = false;
  private levelTimer: ReturnType<typeof setTimeout> | null = null;
  private levelAbort: AbortController | null = null;

  constructor(readonly api: XenioApi, readonly store: Store) {}

  async init(): Promise<void> {
    await this.loadSnapshot();
    this.unsubscribe = this.api.subscribe(this.store.get().snapshot?.seq ?? 0, (e) => this.onEvent(e),
      (stream) => {
        const wasDown = this.store.get().stream === "retrying";
        this.store.set({ stream });
        if (stream === "open" && wasDown) void this.loadSnapshot(); // after an outage: re-read, never assume
      });
    try {
      this.store.set({ settings: await this.api.settings() });
    } catch {
      /* defaults stay; the Settings page shows the error */
    }
  }

  dispose(): void {
    this.unsubscribe?.();
    this.recording?.cancel();
    this.speaker.stop();
    this.speakAbort?.abort();
    this.stopLevelPolling();
  }

  /** XENIO's talk engine is running: the microphone button and spoken replies go through it. */
  get engine(): boolean {
    return this.store.get().snapshot?.voice.engine === true;
  }

  private loadSnapshot(): Promise<void> {
    this.loading ??= (async () => {
      try {
        this.store.loadSnapshot(await this.api.snapshot());
      } catch (error) {
        this.store.set({ loadError: error instanceof Error ? error.message : "XENIO could not be reached" });
      } finally {
        this.loading = null;
      }
    })();
    return this.loading;
  }

  private onEvent(event: XenioEvent): void {
    if (event.type === "stream.reset") {
      void this.loadSnapshot();
      return;
    }
    this.store.apply(event);
    if (event.type === "chat.user" && (event.data as { quiet?: boolean }).quiet) this.quietTurns.add(String(event.data.turn));
    if (event.type === "chat.done") void this.maybeSpeak(event.data as { turn: string; text: string; ok?: boolean });
    if (event.type === "assistant.state") this.syncLevelPolling();
  }

  // ---- text ---------------------------------------------------------------------------------------------------

  /** quiet: typed in the quick chat - the reply stays on screen, never read aloud */
  async send(text: string, via: "text" | "voice" = "text", quiet = false): Promise<boolean> {
    const clean = text.trim();
    if (!clean) {
      this.store.notify({ tone: "attention", title: "Nothing to send", message: "Type or say something first." });
      return false;
    }
    void unlockAudio(); // inside the click: lets the reply be spoken later
    try {
      const { turn } = await this.api.chat(clean, via, quiet);
      if (via === "voice") this.voiceTurns.add(turn);
      return true;
    } catch (error) {
      this.store.notify({ tone: "danger", title: "Not sent", message: error instanceof Error ? error.message : String(error) });
      return false;
    }
  }

  async cancel(): Promise<void> {
    this.stopSpeaking();
    try {
      await this.api.cancel();
    } catch (error) {
      this.store.notify({ tone: "danger", title: "Could not stop", message: (error as Error).message });
    }
  }

  // ---- microphone ---------------------------------------------------------------------------------------------

  micLevel = (): number => this.recording?.level() ?? 0;

  /** the talk engine's level (its microphone while listening, XENIO's voice while speaking), polled while it is in use */
  engineLevel = (): number => this.engineLevelValue;

  get listening(): boolean {
    return this.recording !== null;
  }

  async toggleListening(): Promise<void> {
    if (this.recording) {
      this.recording.stop();
      return;
    }
    if (this.engine) {
      await this.engineListen();
      return;
    }
    void unlockAudio(); // inside the click: Safari plays the spoken reply later only if audio started from a gesture
    this.stopSpeaking();
    this.store.set({ voiceError: null });
    let recording: Recording;
    try {
      recording = await this.recorder();
    } catch (error) {
      this.voiceFailed(error);
      return;
    }
    this.recording = recording;
    this.setVoice({ listening: true });
    let wav: Blob | null = null;
    try {
      wav = await recording.done;
    } catch (error) {
      this.voiceFailed(error);
    } finally {
      this.recording = null;
      this.setVoice({ listening: false });
    }
    if (!wav) return;
    this.setVoice({ transcribing: true });
    try {
      const { text } = await this.api.transcribe(wav);
      if (!text) {
        this.voiceFailed(new VoiceError("silent", "Nothing understandable was heard. Try again, or type it."));
        return;
      }
      await this.send(text, "voice");
    } catch (error) {
      this.voiceFailed(error);
    } finally {
      this.setVoice({ transcribing: false });
    }
  }

  cancelListening(): void {
    this.recording?.cancel();
  }

  private async engineListen(): Promise<void> {
    this.store.set({ voiceError: null });
    try {
      if (this.store.get().assistant?.voice === "listening") await this.api.voiceFinish();
      else await this.api.voiceListen();
    } catch (error) {
      this.voiceFailed(error);
    }
  }

  private syncLevelPolling(): void {
    const phase = this.store.get().assistant?.voice;
    if (phase === "listening" || phase === "speaking") {
      if (!this.levelTimer && !this.levelAbort) this.pollLevel();
    } else {
      this.stopLevelPolling();
    }
  }

  private pollLevel(): void {
    this.levelTimer = null;
    const abort = new AbortController();
    this.levelAbort = abort;
    this.api.voiceLevel(abort.signal).then((r) => { this.engineLevelValue = r.level; }, () => { this.engineLevelValue = 0; })
      .finally(() => {
        if (this.levelAbort !== abort) return;
        this.levelAbort = null;
        const phase = this.store.get().assistant?.voice;
        if (phase === "listening" || phase === "speaking") this.levelTimer = setTimeout(() => this.pollLevel(), 100);
        else this.engineLevelValue = 0;
      });
  }

  private stopLevelPolling(): void {
    if (this.levelTimer) clearTimeout(this.levelTimer);
    this.levelTimer = null;
    this.levelAbort?.abort();
    this.levelAbort = null;
    this.engineLevelValue = 0;
  }

  // ---- XENIO's voice ------------------------------------------------------------------------------------------

  private async maybeSpeak(done: { turn: string; text: string; ok?: boolean }): Promise<void> {
    if (this.quietTurns.delete(done.turn)) return; // typed in the quick chat: on screen only
    if (this.engine || this.silent) return; // XENIO speaks through the Mac's speaker itself, sentence by sentence
    const wanted = this.voiceTurns.delete(done.turn) || this.store.get().settings?.speak_replies === true;
    const voiceReady = this.store.get().snapshot?.voice.tts;
    if (!wanted || !voiceReady || !done.text?.trim()) return;
    this.speakAbort?.abort();
    const abort = new AbortController();
    this.speakAbort = abort;
    try {
      const wav = await this.api.speak(done.text, abort.signal);
      if (abort.signal.aborted) return;
      await this.speaker.play(wav, {
        onStart: () => this.setVoice({ speaking: true }),
        onEnd: () => this.setVoice({ speaking: false }),
      });
    } catch (error) {
      if (abort.signal.aborted) return;
      this.setVoice({ speaking: false });
      this.store.set({ voiceError: error instanceof Error ? error.message : "XENIO's voice failed; the answer is shown as text." });
    }
  }

  stopSpeaking(): void {
    this.speakAbort?.abort();
    this.speaker.stop();
    this.setVoice({ speaking: false });
    if (this.store.get().assistant?.voice === "speaking") void this.api.cancel().catch(() => undefined); // XENIO's own voice
  }

  private setVoice(patch: Partial<{ listening: boolean; transcribing: boolean; speaking: boolean }>): void {
    this.store.set((s) => ({ voice: { ...s.voice, ...patch } }));
  }

  private voiceFailed(error: unknown): void {
    const message = error instanceof VoiceError || error instanceof ApiError || error instanceof Error
      ? error.message : "Voice input failed. Type your request instead.";
    this.store.set({ voiceError: message });
  }
}
