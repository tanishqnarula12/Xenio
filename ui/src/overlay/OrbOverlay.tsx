// The floating orb (inside XENIO Orb, native/orb): XENIO within reach over any app. It shows only what XENIO is
// really doing (the same state machine as Home), the words being heard or said, and any question waiting for a yes -
// answerable right here. Summoned (triple Space), it offers the two ways to talk: Chat opens the quick chat in this
// same panel (the conversation, a message box), Voice listens; the user goes back and forth (the chat's microphone,
// the orb's chat button) and can open Mission Control from the chat. It never plays audio itself: XENIO's talk engine
// speaks through the Mac's speaker.

import { MessageSquare, Mic, MicOff, Square, X } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type PointerEvent } from "react";
import type { Approval } from "../api/types";
import { useOrbState } from "../components/useOrbState";
import { useReducedMotion } from "../motion/motion";
import { Orb } from "../orb/Orb";
import { useApp, useAppState } from "../state/context";
import { listen, post, type NativeMessage } from "./native";
import { QuickChat } from "./QuickChat";

const LINGER_MS = 7000; // after the last activity, the orb stays this long before it tucks away
const CHOICE_MS = 8000; // an unanswered Chat-or-Voice choice tucks away after this
const WIDTH = 400;
const CHOICE_WIDTH = 330;
const COLLAPSED = 96;
const CHAT_WIDTH = 420;
const CHAT_HEIGHT = 580;
const ROUTE = /^\/[a-z]+(\/[A-Za-z0-9_-]+)?$/;

function OverlayApproval({ approval }: { approval: Approval }) {
  const { app } = useApp();
  const [busy, setBusy] = useState(false);
  const decide = async (decision: "approve" | "approve_task" | "deny") => {
    setBusy(true);
    try {
      await app.api.decide(approval.id, decision);
    } catch (e) {
      app.store.notify({ tone: "danger", title: "Not sent", message: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="ov-approval" role="group" aria-label={`Approval needed: ${approval.description}`}>
      <p className="ov-approval__what">XENIO wants to {approval.description}.</p>
      {approval.strict ? (
        <p className="ov-approval__note">High risk: say "confirm", or answer in XENIO.</p>
      ) : (
        <div className="ov-approval__actions">
          {approval.confined && <button type="button" className="ov-btn ov-btn--primary" disabled={busy}
            onClick={() => void decide("approve_task")}>Allow for this task</button>}
          <button type="button" className={`ov-btn ${approval.confined ? "" : "ov-btn--primary"}`} disabled={busy}
            onClick={() => void decide("approve")}>Allow once</button>
          <button type="button" className="ov-btn" disabled={busy} onClick={() => void decide("deny")}>Deny</button>
        </div>
      )}
    </div>
  );
}

export function OrbOverlay() {
  const { app } = useApp();
  const orb = useOrbState();
  const reduced = useReducedMotion();
  const assistant = useAppState((s) => s.assistant);
  const approvals = useAppState((s) => s.approvals);
  const settings = useAppState((s) => s.settings);
  const streaming = useAppState((s) => s.streaming);
  const conversation = useAppState((s) => s.conversation);
  const notices = useAppState((s) => s.notices);
  const engine = useAppState((s) => s.snapshot?.voice.engine === true);
  const box = useRef<HTMLDivElement>(null);
  const [lastActive, setLastActive] = useState(0);  // when something last happened (the linger window)
  // what happened is counted, not timed: a dismissal and new activity in the same millisecond must still show it
  const [seq, setSeq] = useState(0);
  const [dismissedSeq, setDismissedSeq] = useState(-1);
  const bump = useCallback(() => {
    setLastActive(Date.now());
    setSeq((n) => n + 1);
  }, []);
  const [choosingAt, setChoosingAt] = useState(0);  // summoned: Chat or Voice?
  const [chatting, setChatting] = useState(false);  // the quick chat is open (until the user closes it)
  const [, tick] = useState(0);
  const summon = useRef<number | null>(null);
  const press = useRef<{ x: number; y: number } | null>(null);
  const shownAt = useRef(0);

  const busy = Boolean(assistant?.turn) || Boolean(assistant?.voice) || approvals.length > 0 || Boolean(streaming);
  const newestNotice = notices.at(-1);
  const listeningNow = assistant?.voice === "listening";
  const mounted = useRef(false);
  // something new (activity starting or ending, a question, a notice, the wake word) brings the orb back, even after
  // it was dismissed; it lingers a few seconds after the activity ends
  useEffect(() => {
    if (!mounted.current) {
      mounted.current = true;
      if (!busy) return;
    }
    bump();
    if (busy) setChoosingAt(0);  // XENIO is already listening or working: no need to ask how
  }, [busy, approvals.length, newestNotice?.id, listeningNow, bump]);
  useEffect(() => {  // re-check the linger window (and an unanswered choice) once it may have ended
    const timer = setTimeout(() => tick((n) => n + 1), Math.max(LINGER_MS, CHOICE_MS) + 50);
    return () => clearTimeout(timer);
  }, [lastActive, choosingAt]);

  const mode = settings?.orb_overlay_mode ?? "active";
  const recent = Date.now() - lastActive < LINGER_MS;
  const choosing = !chatting && choosingAt > 0 && Date.now() - choosingAt < CHOICE_MS;
  const active = chatting || choosing || ((busy || recent) && seq > dismissedSeq);
  const visible = active || mode === "always";

  // the panel follows the content: the chat, the choice, the expanded orb while active, the orb alone when idle
  // ("always"), hidden otherwise
  useLayoutEffect(() => {
    if (!visible) {
      post({ type: "hide" });
      shownAt.current = 0;
      return;
    }
    const width = chatting ? CHAT_WIDTH : choosing ? CHOICE_WIDTH : active ? WIDTH : COLLAPSED;
    const height = chatting ? CHAT_HEIGHT
      : active ? Math.max(COLLAPSED, Math.ceil(box.current?.scrollHeight ?? COLLAPSED)) : COLLAPSED;
    post({ type: shownAt.current ? "size" : "show", width, height });
    if (!shownAt.current) {
      shownAt.current = Date.now();
      const since = assistant?.voice === "listening" ? assistant.voice_since ?? 0 : 0;
      if (since && shownAt.current - since < 5000) void app.api.overlayMetric("listening_to_orb_ms", shownAt.current - since).catch(() => {});
      if (summon.current) void app.api.overlayMetric("summon_to_orb_ms", shownAt.current - summon.current).catch(() => {});
    }
  });
  // the chat takes the keyboard (the helper makes the panel key without activating itself) and gives it back on close
  useEffect(() => { post({ type: "chat", on: chatting }); }, [chatting]);

  const talk = useCallback(() => {
    setChoosingAt(0);
    setDismissedSeq(-1);
    bump();
    if (assistant?.voice !== "listening" && engine) void app.toggleListening();
  }, [app, assistant?.voice, engine, bump]);
  const openChat = useCallback(() => {
    setChoosingAt(0);
    setChatting(true);
    post({ type: "chat", on: true });  // already open: take the keyboard again
    document.getElementById("composer-input")?.focus();
  }, []);
  const closeChat = useCallback(() => {
    setChatting(false);
    setDismissedSeq(seq);  // tucks away; anything new brings the orb back
  }, [seq]);
  const openApp = useCallback((route?: string) => {
    post(route && ROUTE.test(route) ? { type: "openApp", route } : { type: "openApp" });
  }, []);

  // summon (triple Space / Control-Option-Space, or XENIO itself): ask Chat or Voice, or go straight to the one asked
  // for (by XENIO, or chosen in Settings); with the chat open, a summon puts the cursor back in it
  const summonMode = settings?.orb_summon_mode ?? "choose";
  useEffect(() => listen((message: NativeMessage) => {
    if (message.type === "summon") {
      summon.current = message.at;
      const how = message.mode ?? (summonMode === "chat" ? "chat" : summonMode === "talk" ? "voice" : null);
      if (chatting || how === "chat") openChat();
      else if (how === "voice") talk();
      else {
        setChoosingAt(Date.now());
        setDismissedSeq(-1);
      }
    } else if (message.type === "closeChat") {
      closeChat();
    } else if (message.type === "status") {
      const { type: _type, ...status } = message;
      void app.api.overlayStatus(status).catch(() => {});
    } else if (message.type === "location") {
      void app.api.saveLocation({ label: message.label, lat: message.lat, lon: message.lon, source: "device",
        accuracy_m: message.accuracy_m }).catch(() => {});
    }
  }), [app, chatting, openChat, closeChat, talk, summonMode]);
  useEffect(() => {
    if (summon.current && assistant?.voice === "listening") {
      void app.api.overlayMetric("summon_to_listening_ms", Date.now() - summon.current).catch(() => {});
      summon.current = null;
    }
  }, [app, assistant?.voice]);

  useEffect(() => { post({ type: "ready" }); }, []);
  useEffect(() => {
    if (!settings) return;
    post({ type: "config", shortcut: settings.summon_shortcut ?? "triple_space", interval_ms: settings.summon_interval_ms ?? 450,
      location: settings.device_location ?? false });
  }, [settings]);

  const onDown = (e: PointerEvent<HTMLDivElement>) => {
    press.current = { x: e.screenX, y: e.screenY };
    post({ type: "dragStart" });
  };
  const onUp = (e: PointerEvent<HTMLDivElement>) => {
    const start = press.current;
    press.current = null;
    if (start && Math.hypot(e.screenX - start.x, e.screenY - start.y) < 4) {  // a click, not a drag: talk
      setChoosingAt(0);
      setDismissedSeq(-1);
      bump();
      void app.toggleListening();
    }
  };
  const close = () => {
    setChoosingAt(0);
    setDismissedSeq(seq);
  };

  if (chatting) {
    return <QuickChat onClose={closeChat} onOpenApp={openApp} onDragStart={() => post({ type: "dragStart" })} />;
  }

  const lastUser = [...conversation].reverse().find((m) => m.role === "user");
  const caption = assistant?.voice === "listening" ? "Listening…"
    : streaming?.text ? streaming.text.slice(-160)
    : assistant?.turn && lastUser ? `“${lastUser.text.slice(0, 140)}”`
    : newestNotice && recent ? `${newestNotice.title}: ${newestNotice.message}`.slice(0, 160)
    : orb.sentence;
  const listening = assistant?.voice === "listening";
  const stoppable = Boolean(assistant?.turn) || assistant?.voice === "speaking";

  if (choosing) {
    return (
      <div className="orb-overlay orb-overlay--open orb-overlay--choose" ref={box}>
        <div className="ov-orb ov-orb--choose" onPointerDown={onDown} onPointerUp={onUp} title="Drag to move · click to talk">
          <Orb state={orb.state} label={orb.label} size={64} detail="low" reducedMotion={reduced} level={orb.level} agents={orb.agents} />
        </div>
        <div className="ov-choose" role="group" aria-label="Talk to XENIO">
          <button type="button" className="ov-choice" onClick={openChat}>
            <MessageSquare size={18} aria-hidden="true" /><span>Chat</span>
          </button>
          <button type="button" className="ov-choice" onClick={talk} disabled={!engine}
            title={engine ? "Talk to XENIO" : "XENIO's microphone is not in use: open Chat and use its microphone"}>
            <Mic size={18} aria-hidden="true" /><span>Voice</span>
          </button>
        </div>
        <button type="button" className="ov-icon ov-choose__close" onClick={close} aria-label="Close" title="Close">
          <X size={14} aria-hidden="true" />
        </button>
      </div>
    );
  }

  return (
    <div className={`orb-overlay ${active ? "orb-overlay--open" : ""}`} ref={box}>
      <div className="ov-orb" onPointerDown={onDown} onPointerUp={onUp} title="Drag to move · click to talk">
        <Orb state={orb.state} label={orb.label} size={active ? 84 : 76} detail="low" reducedMotion={reduced} level={orb.level}
          agents={orb.agents} />
      </div>
      {active && (
        <div className="ov-body">
          <div className="ov-head">
            <p className="ov-state" aria-live="polite">{orb.label}</p>
            <div className="ov-actions">
              {engine && <button type="button" className={`ov-icon ${listening ? "ov-icon--on" : ""}`} onClick={() => void app.toggleListening()}
                aria-label={listening ? "Stop listening and send" : "Talk to XENIO"} title={listening ? "Stop listening and send" : "Talk to XENIO"}>
                {listening ? <MicOff size={15} aria-hidden="true" /> : <Mic size={15} aria-hidden="true" />}</button>}
              {stoppable && <button type="button" className="ov-icon" onClick={() => void app.cancel()} aria-label="Stop XENIO" title="Stop XENIO">
                <Square size={13} aria-hidden="true" /></button>}
              <button type="button" className="ov-icon" onClick={openChat} aria-label="Open the chat" title="Chat (type instead)">
                <MessageSquare size={15} aria-hidden="true" /></button>
              <button type="button" className="ov-icon" onClick={close} aria-label="Hide the orb" title="Hide">
                <X size={15} aria-hidden="true" /></button>
            </div>
          </div>
          <p className="ov-caption">{caption}</p>
          {approvals.slice(0, 1).map((a) => <OverlayApproval key={a.id} approval={a} />)}
          {approvals.length > 1 && <p className="ov-more">and {approvals.length - 1} more question{approvals.length > 2 ? "s" : ""} in XENIO</p>}
        </div>
      )}
    </div>
  );
}
