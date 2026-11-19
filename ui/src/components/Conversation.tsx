import { ArrowUp, Check, Copy, FileText, Mic, MicOff, Paperclip, Sparkles, Square, Volume2, VolumeX, X } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState, type ChangeEvent, type ClipboardEvent, type DragEvent, type FormEvent, type KeyboardEvent, type ReactNode } from "react";
import type { Message, ToolActivity, UploadedFile } from "../api/types";
import { ImageCard } from "../images/ImageCard";
import { LessonCard } from "../teach/LessonView";
import { useApp, useAppState } from "../state/context";
import { friendlyTool } from "../orb/machine";
import { ApprovalCard } from "./ApprovalCard";
import { RichText, safeUrl } from "./RichText";
import { Status } from "./ui";
import { useOrbState } from "./useOrbState";
import { XenioMark } from "./XenioMark";

function ActionLine({ line }: { line: string }) {
  const m = /^\s*\[(done|FAILED|declined|NOT CONFIRMED)\]\s*(.*)$/i.exec(line);
  if (!m) return <li className="meta">{line}</li>;
  const kind = m[1]!.toLowerCase();
  const tone = kind === "done" ? "ok" : kind === "failed" ? "danger" : kind === "not confirmed" ? "attention" : "neutral";
  const word = kind === "done" ? "Done" : kind === "failed" ? "Failed" : kind === "not confirmed" ? "Not confirmed" : "Declined";
  return <li><Status tone={tone}>{word}</Status> <span className="action-text">{m[2]}</span></li>;
}

function MessageView({ message, arrived = false }: { message: Message; arrived?: boolean }) {
  if (message.role === "user") {
    return (
      <div className="msg msg--user">
        <p>{message.text}</p>
        {!!message.files?.length && <ul className="msg__files" aria-label="Files added">{message.files.map((f) => <li key={f.id} className="file-chip"><FileText size={13} aria-hidden="true" /><span>{f.name}</span></li>)}</ul>}
        {message.via === "voice" && <span className="meta">Said aloud</span>}
      </div>
    );
  }
  return (
    <div className={`msg msg--assistant ${message.ok === false && !message.cancelled ? "msg--failed" : ""} ${arrived ? "msg--arrived" : ""}`}>
      <span className="msg__mark" aria-hidden="true"><XenioMark size={16} /></span>
      <div className="msg__body">
      {message.cancelled && <Status tone="neutral">Stopped</Status>}
      {message.ok === false && !message.cancelled && <Status tone="danger">This request failed</Status>}
      {message.text ? <RichText text={message.text} /> : !message.cancelled && <p className="meta">No answer came back.</p>}
      {message.attachments?.map((card) => (card.kind === "lesson" ? <LessonCard key={`l${card.id}`} id={Number(card.id)} /> : <ImageCard key={`i${card.id}`} id={String(card.id)} />))}
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
      {message.text && <CopyAnswer text={message.text} />}
      </div>
    </div>
  );
}

/** Copy an answer as it was written. It says so for a moment once it is copied. */
function CopyAnswer({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
    }
  };
  return (
    <div className="msg__tools">
      <button type="button" className="msg__tool" onClick={() => void copy()} aria-label={copied ? "Copied" : "Copy the answer"}>
        {copied ? <Check size={14} aria-hidden="true" /> : <Copy size={14} aria-hidden="true" />}<span>{copied ? "Copied" : "Copy"}</span>
      </button>
    </div>
  );
}

function LiveTools({ tools }: { tools: ToolActivity[] }) {
  if (!tools.length) return null;
  return (
    <ul className="live-tools" aria-label="What XENIO is doing">
      {tools.slice(-4).map((t) => (
        <li key={t.id}>
          <Status tone={t.status === "failed" ? "danger" : t.unconfirmed ? "attention" : t.status === "completed" ? "ok" : "running"}>
            {friendlyTool(t.title.split(":")[0] ?? t.title)}{t.unconfirmed ? " (not confirmed)" : ""}
          </Status>
        </li>
      ))}
    </ul>
  );
}

/** What XENIO is really doing while it answers: the tool it is running, "Writing" once words arrive, else "Thinking". */
export function workingLabel(tools: ToolActivity[], writing: boolean): string {
  const running = [...tools].reverse().find((t) => t.status !== "completed" && t.status !== "failed");
  if (running) {
    const words = friendlyTool(running.title.split(":")[0] ?? running.title);
    return words.charAt(0).toUpperCase() + words.slice(1);
  }
  return writing ? "Writing" : "Thinking";
}

const DOING: ReadonlySet<string> = new Set(["SEARCHING", "EXECUTING", "PLANNING", "ORCHESTRATING", "WAITING_APPROVAL"]);

function WorkingLine({ tools, writing }: { tools: ToolActivity[]; writing: boolean }) {
  const orb = useOrbState(); // the same words as the orb, at the same moment
  const label = DOING.has(orb.state) ? orb.label : workingLabel(tools, writing);
  const [shown, setShown] = useState<{ cur: string; prev: string | null; n: number }>({ cur: label, prev: null, n: 0 });
  useEffect(() => { setShown((s) => (s.cur === label ? s : { cur: label, prev: s.cur, n: s.n + 1 })); }, [label]);
  return (
    <p className="working" role="status" aria-live="polite">
      {shown.prev !== null && <span key={`p${shown.n}`} className="working__label working__label--out" aria-hidden="true">{shown.prev}</span>}
      <span key={`c${shown.n}`} className="working__label working__label--in">{shown.cur}<span className="working__dots" aria-hidden="true"><i /><i /><i /></span></span>
    </p>
  );
}

/** What the reply offers next, one tap away. They go when the next thing starts. */
function NextSteps({ choices, quiet }: { choices: string[]; quiet: boolean }) {
  const { app } = useApp();
  const busy = useAppState((s) => Boolean(s.assistant?.turn));
  if (busy) return null;
  return (
    <div className="next-steps" role="group" aria-label="What next?">
      {choices.map((choice) => <button type="button" key={choice} className="next-step" onClick={() => void app.send(choice, "text", quiet)}>{choice}</button>)}
    </div>
  );
}

export function Transcript({ limit, quiet = false }: { limit?: number; quiet?: boolean }) {
  const conversation = useAppState((s) => s.conversation);
  const streaming = useAppState((s) => s.streaming);
  const tools = useAppState((s) => s.tools);
  const box = useRef<HTMLDivElement>(null);
  const shown = limit ? conversation.slice(-limit) : conversation;
  const known = useRef<Set<string> | null>(null); // answers already there when this opened do not animate in
  if (known.current === null) known.current = new Set(conversation.map((m) => m.id));

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

  // a diagram or a picture finishes after its words: the newest stays in view, with what the reply offers next, unless you have scrolled up to read
  const present = shown.length > 0 || Boolean(streaming);
  useEffect(() => {
    let el: HTMLElement | null = box.current?.parentElement ?? null;
    while (el && el !== document.body && !/(auto|scroll)/.test(getComputedStyle(el).overflowY)) el = el.parentElement;
    if (!box.current || !el || el === document.body) return;
    const scroller = el;
    let following = true;
    const onScroll = () => { following = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80; };
    const observer = new ResizeObserver(() => { if (following) scroller.scrollTop = scroller.scrollHeight; });
    scroller.addEventListener("scroll", onScroll, { passive: true });
    observer.observe(box.current);
    return () => { scroller.removeEventListener("scroll", onScroll); observer.disconnect(); };
  }, [present]);

  if (!shown.length && !streaming) {
    return <p className="transcript__empty">Ask XENIO anything, or tell it what to do on your Mac.</p>;
  }
  return (
    <div className="transcript" ref={box}>
      {shown.map((m) => <MessageView key={m.id} message={m} arrived={m.role === "assistant" && !known.current!.has(m.id)} />)}
      {!streaming && shown.at(-1)?.role === "assistant" && !!shown.at(-1)?.next?.length && <NextSteps choices={shown.at(-1)!.next!} quiet={quiet} />}
      {streaming && (
        <div className="msg msg--assistant msg--live" aria-live="polite" aria-busy="true">
          <span className="msg__mark msg__mark--live" aria-hidden="true"><XenioMark size={16} /></span>
          <div className="msg__body">
            <WorkingLine tools={tools} writing={Boolean(streaming.text)} />
            <LiveTools tools={tools} />
            {streaming.text ? <RichText text={streaming.text} /> : null}
          </div>
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
  const picker = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<UploadedFile[]>([]);
  const [uploading, setUploading] = useState<string[]>([]);
  const [fileError, setFileError] = useState<string | null>(null);
  const [dropping, setDropping] = useState(false);
  const [typing, setTyping] = useState(false);
  const [nudge, setNudge] = useState(false);
  const typingTimer = useRef<number | undefined>(undefined);
  const nudgeTimer = useRef<number | undefined>(undefined);
  useEffect(() => () => { window.clearTimeout(typingTimer.current); window.clearTimeout(nudgeTimer.current); }, []);
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
    if (!text.trim() && !files.length && !sending && !uploading.length) { setNudge(true); window.clearTimeout(nudgeTimer.current); nudgeTimer.current = window.setTimeout(() => setNudge(false), 320); return; } // an empty send gets a small shake, not silence
    if ((!text.trim() && !files.length) || sending || uploading.length) return;
    setSending(true);
    const ok = await app.send(text, "text", quiet, files.map((f) => f.id));
    setSending(false);
    if (ok) { setText(""); setFiles([]); }
    input.current?.focus();
  };

  const addFiles = async (list: File[]) => {
    if (!list.length) return;
    setFileError(null);
    for (const file of list.slice(0, Math.max(0, 5 - files.length))) {
      setUploading((u) => [...u, file.name]);
      try {
        const saved = await app.api.upload(file);
        setFiles((f) => [...f, saved]);
      } catch (error) {
        setFileError(`${file.name}: ${(error as Error).message}`);
      } finally {
        setUploading((u) => u.filter((n) => n !== file.name));
      }
    }
    if (list.length + files.length > 5) setFileError("At most 5 files in one message.");
  };
  const onPick = (e: ChangeEvent<HTMLInputElement>) => { const list = Array.from(e.target.files ?? []); e.target.value = ""; void addFiles(list); };
  const onDrop = (e: DragEvent<HTMLFormElement>) => { e.preventDefault(); setDropping(false); void addFiles(Array.from(e.dataTransfer.files)); };
  const onPaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const pasted = Array.from(e.clipboardData.files);
    if (pasted.length) { e.preventDefault(); void addFiles(pasted); }
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
    <form className={`composer ${dropping ? "composer--drop" : ""}`} onSubmit={submit} aria-label="Talk to XENIO" data-typing={typing ? "" : undefined} data-busy={busy ? "" : undefined}
      onDragOver={(e) => { if (e.dataTransfer.types.includes("Files")) { e.preventDefault(); setDropping(true); } }} onDragLeave={() => setDropping(false)} onDrop={onDrop}>
      {voiceError && <p className="voice-error" role="alert">{voiceError}</p>}
      {fileError && <p className="voice-error" role="alert">{fileError}</p>}
      {(files.length > 0 || uploading.length > 0) && (
        <ul className="composer__files" aria-label="Files to send">
          {files.map((f) => <li key={f.id} className="file-chip"><FileText size={13} aria-hidden="true" /><span>{f.name}</span><button type="button" aria-label={`Remove ${f.name}`} onClick={() => setFiles((all) => all.filter((x) => x.id !== f.id))}><X size={13} aria-hidden="true" /></button></li>)}
          {uploading.map((name) => <li key={name} className="file-chip file-chip--busy" role="status"><span>Adding {name}…</span></li>)}
        </ul>
      )}
      {hint && <p className="meta composer__hint" id="composer-hint">{hint}</p>}
      <div className="composer__row" data-nudge={nudge ? "" : undefined}>
        <span className="composer__aurora" aria-hidden="true" />
        <Sparkles className="composer__spark" size={16} aria-hidden="true" />
        <label htmlFor="composer-input" className="sr-only">Message XENIO</label>
        <textarea id="composer-input" ref={input} rows={1} value={text} placeholder="Ask or tell XENIO…"
          onChange={(e) => { setText(e.target.value); setTyping(true); window.clearTimeout(typingTimer.current); typingTimer.current = window.setTimeout(() => setTyping(false), 320); }} onKeyDown={onKey} onPaste={onPaste} maxLength={4000}
          aria-describedby={hint ? "composer-hint" : undefined} />
        <input ref={picker} type="file" multiple hidden aria-label="Add files" onChange={onPick} />
        <button type="button" className="icon-btn" onClick={() => picker.current?.click()} aria-label="Add files" title="Add files: PDFs, documents, photos, code"><Paperclip size={18} aria-hidden="true" /></button>
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
          <button type="submit" className="send-btn" data-ready={text.trim() || files.length ? "" : undefined} disabled={(!text.trim() && !files.length) || uploading.length > 0 || sending || !connected || paused} aria-label="Send">
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
