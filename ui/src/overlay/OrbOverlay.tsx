// The floating orb (inside XENIO Orb, native/orb): XENIO within reach over any app. It shows only what XENIO is
// really doing (the same state machine as Home), the words being heard or said, and any question waiting for a yes -
// answerable right here. Summoned (triple Space), it offers the two ways to talk: Chat opens the quick chat in this
// same panel (the conversation, a message box), Voice listens; the user goes back and forth (the chat's microphone,
// the orb's chat button) and can open Mission Control from the chat. It never plays audio itself: XENIO's talk engine
// speaks through the Mac's speaker.

import { ArrowUp, AudioLines, EyeOff, MessageSquare, Mic, MicOff, Music2, Plus, SquarePen, Square, X } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type PointerEvent } from "react";
import type { Approval } from "../api/types";
import { workingLabel } from "../components/Conversation";
import { useOrbState } from "../components/useOrbState";
import { useReducedMotion } from "../motion/motion";
import { Orb } from "../orb/Orb";
import { useApp, useAppState } from "../state/context";
import { inHelper, listen, post, type NativeMessage } from "./native";
import { QuickChat } from "./QuickChat";

const LINGER_MS = 7000; // after the last activity, the orb stays this long before it tucks away
const WIDTH = 400;
const CHOICE_WIDTH = 330;
const COLLAPSED = 96;
const CHAT_WIDTH = 420;
const CHAT_HEIGHT = 580;
const ROUTE = /^\/[a-z]+(\/[A-Za-z0-9_-]+)?$/;
// the orb on its own, floating like a pet: the orb, and under it a Liquid Glass deck (chat, voice) that opens on hover
const PET_W = 232;
const PET_H = 222;
const PET_ORB = 232; // the drawing: the sphere fills 72% of it, the rest is room for its particles to stream out
const SPHERE = 0.72;
const DECK_OPEN = { w: 220, h: 48, r: 24 };
const DECK_SHUT = { w: 40, h: 6, r: 3 };
const DECK_TOP = 166; // right under the sphere, as close as the owl's
const COMPOSE_W = 340; // the glass chat pill under the orb

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
  const [hovered, setHovered] = useState(false);
  const movers = useRef(new Set<(dx: number, dy: number) => void>());
  // music mode: what the Mac is playing moves the orb (its loudness swells it, each beat sends a ring of light through it)
  const [music, setMusic] = useState(false);
  const musicLevel = useRef(0);
  const musicSource = useCallback(() => musicLevel.current, []);
  const beaters = useRef(new Set<() => void>());
  const beats = useCallback((handler: () => void) => {
    beaters.current.add(handler);
    return () => { beaters.current.delete(handler); };
  }, []);
  const moves = useCallback((handler: (dx: number, dy: number) => void) => {
    movers.current.add(handler);
    return () => { movers.current.delete(handler); };
  }, []);

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
  }, [busy, approvals.length, newestNotice?.id, listeningNow, bump]);
  useEffect(() => {  // re-check the linger window (and an unanswered choice) once it may have ended
    const timer = setTimeout(() => tick((n) => n + 1), LINGER_MS + 50);
    return () => clearTimeout(timer);
  }, [lastActive, choosingAt]);

  const mode = settings?.orb_overlay_mode ?? "active";
  const recent = Date.now() - lastActive < LINGER_MS;
  // summoned, it stays until the person hides it: it never tucks itself away
  const choosing = !chatting && choosingAt > 0;
  const active = chatting || choosing || ((busy || recent) && seq > dismissedSeq);
  const [away, setAway] = useState(false);  // XENIO's own window is out of sight
  const [petHidden, setPetHidden] = useState(false);  // the person hid the orb: it stays hidden until summoned, or the window goes away again
  const visible = active || mode === "always" || (away && !petHidden);
  // the orb on its own: summoned, or resting when it is always shown. While XENIO works it is the card with the caption
  // the orb on its own: summoned; or XENIO's window is minimized, closed or hidden (then it stays, in its current phase,
  // until hidden); or resting when it is always shown. A question waiting for a yes needs the card and its buttons
  const pet = !chatting && approvals.length === 0 && (choosing || (away && !petHidden) || (visible && !active));
  const deckOpen = hovered;  // the deck is there only while the pointer is
  const [composing, setComposing] = useState(false);  // the glass chat pill under the orb
  const [draft, setDraft] = useState("");
  const [askedFrom, setAskedFrom] = useState(-1);  // where the conversation stood when the pill sent its question
  const [nudged, setNudged] = useState(false);
  const pillRef = useRef<HTMLFormElement>(null);
  const previewRef = useRef<HTMLButtonElement>(null);
  const phaseRef = useRef<HTMLSpanElement>(null);
  const glassSent = useRef("");
  const working = Boolean(assistant?.turn) || Boolean(assistant?.voice) || Boolean(streaming);

  // the panel follows the content: the chat, the choice, the expanded orb while active, the orb alone when idle
  // ("always"), hidden otherwise
  useLayoutEffect(() => {
    if (!visible) {
      post({ type: "hide" });
      shownAt.current = 0;
      return;
    }
    const width = chatting ? CHAT_WIDTH : pet && composing ? COMPOSE_W : pet ? PET_W : choosing ? CHOICE_WIDTH : active ? WIDTH : COLLAPSED;
    const height = chatting ? CHAT_HEIGHT : pet && composing ? Math.ceil(box.current?.scrollHeight ?? PET_H) : pet ? PET_H
      : active ? Math.max(COLLAPSED, Math.ceil(box.current?.scrollHeight ?? COLLAPSED)) : COLLAPSED;
    post({ type: shownAt.current ? "size" : "show", width, height });
    if (!shownAt.current) {
      shownAt.current = Date.now();
      const since = assistant?.voice === "listening" ? assistant.voice_since ?? 0 : 0;
      if (since && shownAt.current - since < 5000) void app.api.overlayMetric("listening_to_orb_ms", shownAt.current - since).catch(() => {});
      if (summon.current) void app.api.overlayMetric("summon_to_orb_ms", shownAt.current - summon.current).catch(() => {});
    }
  });
  // the orb floats on its own (no card), with real Liquid Glass behind the deck; the card comes back for the chat and the caption
  const glassChat = chatting && inHelper();  // the whole chat, expanded, in Liquid Glass too
  useEffect(() => {
    document.documentElement.classList.toggle("orb-view--pet", pet || glassChat);
    document.documentElement.classList.toggle("orb-view--glass-chat", glassChat);
    post({ type: "surface", glass: pet || glassChat });
  }, [pet, glassChat]);
  // real Liquid Glass exactly behind what the page shows: the deck on hover, the phase while XENIO works, a handle at rest,
  // and in the chat pill the pill and its answer
  useLayoutEffect(() => {
    type Shape = { x: number; y: number; w: number; h: number; r: number; style?: "tinted" };
    if (glassChat) {
      const key = `chat:${CHAT_WIDTH}x${CHAT_HEIGHT}`;
      if (key !== glassSent.current) {
        glassSent.current = key;
        post({ type: "glass", shapes: [{ x: 0, y: 0, w: CHAT_WIDTH, h: CHAT_HEIGHT, r: 24, style: "tinted" }] });
      }
      return;
    }
    if (!pet) { glassSent.current = ""; return; }
    const shapes: Shape[] = [];
    const of = (el: HTMLElement | null, r: number) => { if (el) shapes.push({ x: el.offsetLeft, y: el.offsetTop, w: el.offsetWidth, h: el.offsetHeight, r }); };
    if (composing) {
      of(previewRef.current, 18);
      of(pillRef.current, 26);
    } else if (deckOpen) {
      shapes.push({ x: (PET_W - DECK_OPEN.w) / 2, y: DECK_TOP, w: DECK_OPEN.w, h: DECK_OPEN.h, r: DECK_OPEN.r });
    } else if (working && phaseRef.current) {
      const w = phaseRef.current.offsetWidth + 26;
      shapes.push({ x: (PET_W - w) / 2, y: DECK_TOP + 10, w, h: 28, r: 14 });
    } else {
      shapes.push({ x: (PET_W - DECK_SHUT.w) / 2, y: DECK_TOP + (DECK_OPEN.h - DECK_SHUT.h) / 2, w: DECK_SHUT.w, h: DECK_SHUT.h, r: DECK_SHUT.r });
    }
    const key = JSON.stringify(shapes);
    if (key !== glassSent.current) {
      glassSent.current = key;
      post({ type: "glass", shapes });
    }
  });
  // the chat takes the keyboard (the helper makes the panel key without activating itself) and gives it back on close
  useEffect(() => { post({ type: "chat", on: chatting || composing }); }, [chatting, composing]);

  const talk = useCallback(() => {
    setDismissedSeq(-1);
    bump();
    if (assistant?.voice !== "listening" && engine) void app.toggleListening();
  }, [app, assistant?.voice, engine, bump]);
  const compose = useCallback(() => {  // the deck's Chat: a glass pill under the orb, not the whole chat
    setComposing(true);
    setHovered(false);
    post({ type: "chat", on: true });
    requestAnimationFrame(() => document.getElementById("pet-input")?.focus());
  }, []);
  const stopComposing = useCallback(() => {
    setComposing(false);
    setDraft("");
    setAskedFrom(-1);
  }, []);
  const openChat = useCallback(() => {
    setComposing(false);
    setChatting(true);
    post({ type: "chat", on: true });  // already open: take the keyboard again
    document.getElementById("composer-input")?.focus();
  }, []);
  const closeChat = useCallback(() => {
    setChatting(false);
    setChoosingAt(0);
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
        setPetHidden(false);
      }
    } else if (message.type === "app") {
      setAway(message.away);
      if (message.away) setPetHidden(false);  // the window went away: the orb comes back, even if it was hidden before
    } else if (message.type === "musicLevel") {
      musicLevel.current = Math.max(message.level * 0.75, message.bass);
      if (message.beat) beaters.current.forEach((handler) => handler());
    } else if (message.type === "music") {
      setMusic(message.on);
      if (!message.on) musicLevel.current = 0;
      if (message.error) app.store.notify({ tone: "danger", title: "Music mode is off", message: /permission|declined|not authori[sz]ed|TCC/i.test(message.error)
        ? "XENIO Orb needs Screen & System Audio Recording (System Settings › Privacy & Security) to hear what the Mac plays." : message.error });
    } else if (message.type === "moved") {
      movers.current.forEach((handler) => handler(message.dx, message.dy));
    } else if (message.type === "hover") {
      setHovered(message.on);
    } else if (message.type === "closeChat") {
      if (composing) stopComposing();
      else closeChat();
    } else if (message.type === "status") {
      const { type: _type, ...status } = message;
      void app.api.overlayStatus(status).catch(() => {});
    } else if (message.type === "location") {
      void app.api.saveLocation({ label: message.label, lat: message.lat, lon: message.lon, source: "device",
        accuracy_m: message.accuracy_m }).catch(() => {});
    }
  }), [app, chatting, composing, openChat, closeChat, stopComposing, talk, summonMode]);
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
      setDismissedSeq(-1);
      bump();
      void app.toggleListening();
    }
  };
  const toggleMusic = () => post({ type: "music", on: !music });
  const close = () => {
    if (music) post({ type: "music", on: false });  // hidden: nothing to move, so nothing to listen to
    setChoosingAt(0);
    setDismissedSeq(seq);
    setPetHidden(true);  // hidden by the person: it stays hidden until summoned again
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

  if (pet) {
    const answers = conversation.slice(askedFrom < 0 ? conversation.length : askedFrom).filter((m) => m.role === "assistant");
    const answer = streaming?.text || answers.at(-1)?.text || "";
    const asked = askedFrom >= 0;
    const send = () => {
      const text = draft.trim();
      if (!text) {
        setNudged(true);
        window.setTimeout(() => setNudged(false), 320);
        return;
      }
      setAskedFrom(conversation.length);
      setDraft("");
      void app.send(text, "text", true);  // answered on screen: the pill does not speak
    };
    return (
      <div className={`pet ${composing ? "pet--compose" : ""} ${deckOpen ? "pet--open" : ""} ${inHelper() ? "pet--native" : ""}`} ref={box}
        onPointerEnter={() => setHovered(true)} onPointerLeave={() => setHovered(false)}>
        <div className="pet__orb" aria-hidden="false">
          <Orb state={orb.state} label={orb.label} size={PET_ORB} detail="low" reducedMotion={reduced} level={orb.level ?? (music ? musicSource : null)}
            agents={orb.agents} moves={moves} beats={beats} vivid scale={SPHERE} />
          <span className="pet__hit" onPointerDown={onDown} onPointerUp={onUp} title="Drag to move · click to talk" />
        </div>
        {composing ? (
          <>
            {asked && (
              <button type="button" className="pet__preview" ref={previewRef} onClick={openChat} aria-label="Open the whole chat">
                <span className="pet__preview-text">{answer || `${workingLabel([], false)}…`}</span>
                <span className="pet__preview-hint">{working ? orb.label : "Open the chat"}</span>
              </button>
            )}
            <form className={`pet__pill ${nudged ? "is-nudged" : ""}`} ref={pillRef} aria-label="Ask XENIO"
              onSubmit={(e) => { e.preventDefault(); send(); }}>
              <button type="button" className="pet__pill-btn" onClick={openChat} aria-label="Open the whole chat" title="The whole chat: files, history">
                <Plus size={18} aria-hidden="true" />
              </button>
              <input id="pet-input" className="pet__input" value={draft} placeholder="Ask XENIO…" aria-label="Ask XENIO" autoComplete="off" maxLength={4000}
                onChange={(e) => setDraft(e.target.value)} onKeyDown={(e) => { if (e.key === "Escape") { e.preventDefault(); stopComposing(); } }} />
              <button type="submit" className="pet__send" aria-label="Send" disabled={!draft.trim()} data-ready={draft.trim() ? "" : undefined}>
                <ArrowUp size={17} aria-hidden="true" />
              </button>
            </form>
          </>
        ) : (
          <div className="pet__deck" role="group" aria-label="Talk to XENIO" style={{ top: DECK_TOP }}
            onFocus={() => setHovered(true)} onBlur={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setHovered(false); }}>
            <span className="pet__handle" aria-hidden="true" />
            {working && !deckOpen && <span className="pet__phase" ref={phaseRef} aria-live="polite">{orb.label}</span>}
            <button type="button" className="pet__btn" onClick={compose} aria-label="Chat" title="Chat" tabIndex={deckOpen ? 0 : -1}>
              <SquarePen size={19} aria-hidden="true" />
            </button>
            <span className="pet__divider" aria-hidden="true" />
            <button type="button" className="pet__btn" onClick={talk} disabled={!engine} aria-label="Voice" tabIndex={deckOpen ? 0 : -1}
              title={engine ? "Talk to XENIO" : "XENIO's microphone is not in use: open Chat and use its microphone"}>
              <AudioLines size={19} aria-hidden="true" />
            </button>
            <span className="pet__divider" aria-hidden="true" />
            <button type="button" className={`pet__btn ${music ? "pet__btn--on" : ""}`} onClick={toggleMusic} aria-pressed={music} aria-label="Music mode"
              title={music ? "Music mode: on (the orb moves with what your Mac plays)" : "Music mode: the orb moves with what your Mac plays"} tabIndex={deckOpen ? 0 : -1}>
              <Music2 size={18} aria-hidden="true" />
            </button>
            <span className="pet__divider" aria-hidden="true" />
            <button type="button" className="pet__btn" onClick={close} aria-label="Hide the orb" title="Hide" tabIndex={deckOpen ? 0 : -1}>
              <EyeOff size={18} aria-hidden="true" />
            </button>
          </div>
        )}
      </div>
    );
  }

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
