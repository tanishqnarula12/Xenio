import { ArrowUp, Mic, MicOff, Square, Volume2, VolumeX } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState, type FormEvent, type KeyboardEvent, type ReactNode } from "react";
import type { Message, ToolActivity } from "../api/types";
import { useApp, useAppState } from "../state/context";
import { friendlyTool } from "../orb/machine";
import { ApprovalCard } from "./ApprovalCard";
import { RichText, safeUrl } from "./RichText";
import { Status } from "./ui";

function ActionLine({ line }: { line: string }) {
  const m = /^\s*\[(done|FAILED|declined)\]\s*(.*)$/i.exec(line);
  if (!m) return <li className="meta">{line}</li>;
  const kind = m[1]!.toLowerCase();
  const tone = kind === "done" ? "ok" : kind === "failed" ? "danger" : "neutral";
  const word = kind === "done" ? "Done" : kind === "failed" ? "Failed" : "Declined";
  return <li><Status tone={tone}>{word}</Status> <span className="action-text">{m[2]}</span></li>;
}

function MessageView({ message }: { message: Message }) {
  if (message.role === "user") {
    return (
      <div className="msg msg--user">
        <p>{message.text}</p>
        {message.via === "voice" && <span className="meta">Said aloud</span>}
      </div>
    );
  }
  return (
    <div className={`msg msg--assistant ${message.ok === false && !message.cancelled ? "msg--failed" : ""}`}>
      {message.cancelled && <Status tone="neutral">Stopped</Status>}
      {message.ok === false && !message.cancelled && <Status tone="danger">This request failed</Status>}
      {message.text ? <RichText text={message.text} /> : !message.cancelled && <p className="meta">No answer came back.</p>}
      {!!message.actions?.length && <ul className="actions" aria-label="What XENIO did">{message.actions.map((a, i) => <ActionLine key={i} line={a} />)}</ul>}
      {!!message.sources?.length && (
        <ul className="sources" aria-label="Sources">
          {message.sources.slice(0, 3).map((s, i) => {
            const url = safeUrl(s.url);
            return <li key={i}>{url ? <a href={url} target="_blank" rel="noopener noreferrer">{s.title || url}</a> : s.title}</li>;
          })}
          {message.sources.length > 3 && <li className="meta sources__more">and {message.sources.length - 3} more</li>}
        </ul>
      )}
    </div>
  );
}

function LiveTools({ tools }: { tools: ToolActivity[] }) {
  if (!tools.length) return null;
  return (
    <ul className="live-tools" aria-label="What XENIO is doing">
      {tools.slice(-4).map((t) => (
        <li key={t.id}>
          <Status tone={t.status === "failed" ? "danger" : t.status === "completed" ? "ok" : "running"}>
            {friendlyTool(t.title.split(":")[0] ?? t.title)}
          </Status>
        </li>
      ))}
    </ul>
  );
}

export function Transcript({ limit }: { limit?: number }) {
  const conversation = useAppState((s) => s.conversation);
  const streaming = useAppState((s) => s.streaming);
  const tools = useAppState((s) => s.tools);
  const box = useRef<HTMLDivElement>(null);
  const shown = limit ? conversation.slice(-limit) : conversation;

  // keep the newest words in view by scrolling the transcript's own scroll area - never the page (that would push
  // the orb and the rest of the screen away)
  useLayoutEffect(() => {
    let el: HTMLElement | null = box.current;
    while (el && !(el.scrollHeight > el.clientHeight && /(auto|scroll)/.test(getComputedStyle(el).overflowY))) {
      if (el === document.body) return;
      el = el.parentElement;
    }
    if (el && el !== document.body && el !== document.documentElement) el.scrollTop = el.scrollHeight;
  }, [shown.length, streaming?.text]);

  if (!shown.length && !streaming) {
    return <p className="transcript__empty">Ask XENIO anything, or tell it what to do on your Mac.</p>;
  }
  return (
    <div className="transcript" ref={box}>
      {shown.map((m) => <MessageView key={m.id} message={m} />)}
      {streaming && (
        <div className="msg msg--assistant msg--live" aria-live="polite" aria-busy="true">
          <LiveTools tools={tools} />
          {streaming.text ? <RichText text={streaming.text} /> : null}
        </div>
      )}
    </div>
  );
}

export function PendingApprovals({ compact = false }: { compact?: boolean }) {
  const approvals = useAppState((s) => s.approvals);
  if (!approvals.length) return null;
  return (
    <div className="pending-approvals" aria-live="assertive">
      {approvals.map((a) => <ApprovalCard key={a.id} approval={a} compact={compact} />)}
    </div>
  );
}

function titleCase(phrase: string): string {
  return phrase.replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * quiet: the quick chat's composer - typed requests are answered on screen only (XENIO still speaks spoken ones).
 * idleHint replaces the line under the box while nothing is being heard or said.
 */
export function Composer({ autoFocus = false, quiet = false, idleHint }: { autoFocus?: boolean; quiet?: boolean; idleHint?: ReactNode }) {
  const { app } = useApp();
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const assistant = useAppState((s) => s.assistant);
  const voice = useAppState((s) => s.voice);
  const voiceError = useAppState((s) => s.voiceError);
  const voiceInfo = useAppState((s) => s.snapshot?.voice);
  const speakReplies = useAppState((s) => s.settings?.speak_replies ?? false);
  const input = useRef<HTMLTextAreaElement>(null);
  const busy = Boolean(assistant?.turn);
  const engine = voiceInfo?.engine === true;
  const sttReady = engine || (voiceInfo?.stt ?? false);
  const listening = voice.listening || assistant?.voice === "listening";
  const transcribing = voice.transcribing || assistant?.voice === "transcribing";
  const speaking = voice.speaking || assistant?.voice === "speaking";
  const wakePhrase = voiceInfo?.wake ? titleCase(voiceInfo.wake_phrase ?? "hey xenio") : null;
  const connected = assistant?.connection === "connected";
  const paused = Boolean(assistant?.paused);

  useEffect(() => {
    if (autoFocus) input.current?.focus();
  }, [autoFocus]);

  const submit = async (e?: FormEvent) => {
    e?.preventDefault();
    if (!text.trim() || sending) return;
    setSending(true);
    const ok = await app.send(text, "text", quiet);
    setSending(false);
    if (ok) setText("");
    input.current?.focus();
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void submit();
    }
  };

  const hint = !connected ? "XENIO is not connected; your message cannot be sent yet."
    : paused ? "XENIO is paused. Resume it to send."
    : busy ? "XENIO is answering. Stop it, or wait for the answer." : null;

  return (
    <form className="composer" onSubmit={submit} aria-label="Talk to XENIO">
      {voiceError && <p className="voice-error" role="alert">{voiceError}</p>}
      {hint && <p className="meta composer__hint" id="composer-hint">{hint}</p>}
      <div className="composer__row">
        <label htmlFor="composer-input" className="sr-only">Message XENIO</label>
        <textarea id="composer-input" ref={input} rows={1} value={text} placeholder="Ask or tell XENIO…"
          onChange={(e) => setText(e.target.value)} onKeyDown={onKey} maxLength={4000}
          aria-describedby={hint ? "composer-hint" : undefined} />
        {speaking ? (
          <button type="button" className="icon-btn" onClick={() => app.stopSpeaking()} aria-label="Stop speaking" title="Stop speaking">
            <VolumeX size={18} aria-hidden="true" />
          </button>
        ) : (
          <button type="button" className={`icon-btn ${listening ? "icon-btn--active" : ""}`}
            onClick={() => void app.toggleListening()} aria-pressed={listening}
            disabled={!sttReady || transcribing || (!connected && !listening)}
            aria-label={listening ? "Stop listening and send" : sttReady ? "Speak to XENIO" : "Voice input is not set up"}
            title={listening ? "Stop listening and send" : sttReady ? "Speak to XENIO" : "Voice input is not set up in XENIO"}>
            {listening ? <MicOff size={18} aria-hidden="true" /> : <Mic size={18} aria-hidden="true" />}
          </button>
        )}
        {busy ? (
          <button type="button" className="icon-btn icon-btn--stop" onClick={() => void app.cancel()} aria-label="Stop XENIO" title="Stop XENIO">
            <Square size={16} aria-hidden="true" />
          </button>
        ) : (
          <button type="submit" className="send-btn" disabled={!text.trim() || sending || !connected || paused} aria-label="Send">
            <ArrowUp size={18} aria-hidden="true" />
          </button>
        )}
      </div>
      <p className="meta composer__foot">
        {listening ? "Listening… stop talking to send, or press the microphone again."
          : transcribing ? "Turning your words into text…"
          : speaking ? "Speaking. Say \u201cXenio, stop\u201d or press the speaker button to stop."
          : idleHint ? idleHint
          : wakePhrase ? <>Say <strong>“{wakePhrase}”</strong> or press the microphone to talk{speakReplies ? " \u00b7 replies are spoken" : ""}</>
          : speakReplies ? <><Volume2 size={13} aria-hidden="true" /> Replies are spoken aloud</> : "Enter to send, Shift+Enter for a new line"}
      </p>
    </form>
  );
}
