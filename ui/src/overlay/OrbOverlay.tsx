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
// the whole chat can be resized from its corner grip, within these, and keeps the size you gave it
const CHAT_MIN = { w: 360, h: 420 };
const CHAT_MAX = { w: 960, h: 1100 };
const CHAT_SIZE_KEY = "xenio.orb.chatSize";
const GLASS_INSET = 10; // the glass sits inside the window's edge, so its rim and shadow round off instead of being cut square
const CLOSE_MS = 230; // the whole chat shrinks back into the orb before the orb takes its place
const clampChat = (w: number, h: number) => ({ w: Math.round(Math.min(CHAT_MAX.w, Math.max(CHAT_MIN.w, w))), h: Math.round(Math.min(CHAT_MAX.h, Math.max(CHAT_MIN.h, h))) });
function savedChatSize() {
  try {
    const saved = JSON.parse(localStorage.getItem(CHAT_SIZE_KEY) || "null") as { w?: number; h?: number } | null;
    if (saved && typeof saved.w === "number" && typeof saved.h === "number") return clampChat(saved.w, saved.h);
  } catch { /* no storage here: the usual size */ }
  return { w: CHAT_WIDTH, h: CHAT_HEIGHT };
}
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
const HOVER_GRACE_MS = 260; // the deck waits this long after the pointer leaves: passing over the orb does not flicker it
const LEAVE_MS = 240; // the pill melts back into the handle before the window shrinks
const IDLE_AFTER_MS = 45_000; // nothing happening this long: the orb starts to play on its own
const THREAD_FRESH_MS = 5 * 60_000; // a thread this quiet starts afresh with the next request
const THREAD_LINES = 4; // the thread under the orb shows the last few lines; the whole chat has the rest

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
  const [chatSize, setChatSize] = useState(savedChatSize);
  const [chatClosing, setChatClosing] = useState(false);  // shrinking back into the orb
  const closeTimer = useRef(0);
  const fromPet = useRef(false);  // the whole chat was opened from the floating orb: closing it goes back to the orb
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
  // and once the orb floats, it stays the orb while XENIO listens, works and answers: the words build up in one thread
  // under it instead of a new card each time (and no second orb has to start up, which was a visible hitch)
  const wasPet = useRef(false);
  const pet = !chatting && approvals.length === 0 && (choosing || (away && !petHidden) || (visible && (!active || wasPet.current)));
  wasPet.current = pet;
  const deckOpen = hovered;  // the deck is there only while the pointer is
  const [composing, setComposing] = useState(false);  // the glass chat pill under the orb
  const [draft, setDraft] = useState("");
  const [threadFrom, setThreadFrom] = useState(-1);  // where the orb's thread starts in the conversation (-1: no thread yet)
  const threadAt = useRef(0);  // when the thread last changed
  // the pill grows out of the deck (entering: its glass starts where the deck was) and melts back into the handle (leaving)
  const [entering, setEntering] = useState(false);
  const [leaving, setLeaving] = useState(false);
  const leaveTimer = useRef(0);
  const hoverTimer = useRef(0);
  const lastWidth = useRef(0);
  const widthJumped = useRef(false);
  // the deck opens the moment the pointer arrives and closes a moment after it leaves
  const hover = useCallback((on: boolean) => {
    window.clearTimeout(hoverTimer.current);
    if (on) setHovered(true);
    else hoverTimer.current = window.setTimeout(() => setHovered(false), HOVER_GRACE_MS);
  }, []);
  useEffect(() => () => { window.clearTimeout(hoverTimer.current); window.clearTimeout(leaveTimer.current); }, []);
  useEffect(() => {
    if (!entering) return;
    const id = window.setTimeout(() => setEntering(false), 34);  // two frames at the deck's place, then the morph
    return () => window.clearTimeout(id);
  }, [entering]);
  const [nudged, setNudged] = useState(false);
  const pillRef = useRef<HTMLFormElement>(null);
  const previewRef = useRef<HTMLButtonElement>(null);
  const phaseRef = useRef<HTMLSpanElement>(null);
  const glassSent = useRef("");
  const sizeSent = useRef("");
  const working = Boolean(assistant?.turn) || Boolean(assistant?.voice) || Boolean(streaming);
  // the thread: begun by the first request while the orb floats, typed or said, and kept (Esc too) until it goes quiet
  const startThread = useCallback((at: number) => {
    if (threadFrom < 0 || Date.now() - threadAt.current > THREAD_FRESH_MS) setThreadFrom(at);
    threadAt.current = Date.now();
  }, [threadFrom]);
  const seenLength = useRef(conversation.length);
  useEffect(() => {  // a spoken request (or one from elsewhere) joins the thread too, the moment its words arrive
    const before = seenLength.current;
    seenLength.current = conversation.length;
    const added = conversation.length - before;
    // a request and its answer, not the history loading in
    if (pet && added > 0 && added <= 3 && conversation.slice(before).some((m) => m.role === "user")) startThread(before);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversation.length]);
  // the thread lingers a few seconds after its last line, then the orb tucks it away
  const [threadSeen, setThreadSeen] = useState(0);
  useEffect(() => {
    if (threadFrom < 0) return;
    threadAt.current = Date.now();
    setThreadSeen(Date.now());
    const id = window.setTimeout(() => tick((n) => n + 1), LINGER_MS + 50);
    return () => window.clearTimeout(id);
  }, [conversation.length, threadFrom]);
  const thread = threadFrom < 0 ? [] : conversation.slice(threadFrom).filter((m) => m.role === "user" || m.role === "assistant");
  // the orb answering without the pill open (a spoken request): the same thread shows under it, then tucks away
  const talking = pet && !composing && (thread.length > 0 || Boolean(streaming?.text)) && (working || recent || Date.now() - threadSeen < LINGER_MS);
  // idle for a long while: the orb plays on its own (never while working, in music mode, or with the deck or pill open)
  const [idle, setIdle] = useState(false);
  useEffect(() => {
    setIdle(false);
    if (!pet || working || music || composing || hovered) return;
    const id = window.setTimeout(() => setIdle(true), IDLE_AFTER_MS);
    return () => window.clearTimeout(id);
  }, [pet, working, music, composing, hovered, lastActive, conversation.length]);

  // the panel follows the content: the chat, the choice, the expanded orb while active, the orb alone when idle
  // ("always"), hidden otherwise
  useLayoutEffect(() => {
    if (!visible) {
      post({ type: "hide" });
      shownAt.current = 0;
      sizeSent.current = "";
      return;
    }
    const width = chatting ? chatSize.w : pet && (composing || talking) ? COMPOSE_W : pet ? PET_W : choosing ? CHOICE_WIDTH : active ? WIDTH : COLLAPSED;
    const height = chatting ? chatSize.h : pet && (composing || talking) ? Math.ceil(box.current?.scrollHeight ?? PET_H) : pet ? PET_H
      : active ? Math.max(COLLAPSED, Math.ceil(box.current?.scrollHeight ?? COLLAPSED)) : COLLAPSED;
    if (pet && lastWidth.current && width !== lastWidth.current) widthJumped.current = true;
    lastWidth.current = width;
    const sizeKey = `${width}x${height}:${pet}`;  // emptied on hide, so the next show is always sent
    if (sizeKey !== sizeSent.current) {  // every streamed word renders the page; the window only moves when its size does
      sizeSent.current = sizeKey;
      post({ type: shownAt.current ? "size" : "show", width, height, ...(pet ? { anchor: "top-center" as const } : {}) });
    }
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
      // closing: the glass draws in to a circle where the orb sits, then the orb takes its place
      const orbSide = Math.round(PET_ORB * SPHERE * 0.62);
      const shape = chatClosing
        ? { x: Math.round(chatSize.w / 2 - orbSide / 2), y: Math.round(PET_ORB / 2 - orbSide / 2), w: orbSide, h: orbSide, r: orbSide / 2, style: "tinted" as const }
        : { x: GLASS_INSET, y: GLASS_INSET, w: chatSize.w - 2 * GLASS_INSET, h: chatSize.h - 2 * GLASS_INSET, r: 22, style: "tinted" as const };
      const key = `chat:${JSON.stringify(shape)}`;
      if (key !== glassSent.current) {
        glassSent.current = key;
        post({ type: "glass", shapes: [shape] });
      }
      return;
    }
    if (!pet) { glassSent.current = ""; return; }
    const shapes: Shape[] = [];
    const of = (el: HTMLElement | null, r: number) => { if (el) shapes.push({ x: el.offsetLeft, y: el.offsetTop, w: el.offsetWidth, h: el.offsetHeight, r }); };
    if (composing && entering) {  // the glass starts as the deck, where it was on screen
      shapes.push({ x: (COMPOSE_W - DECK_OPEN.w) / 2, y: DECK_TOP, w: DECK_OPEN.w, h: DECK_OPEN.h, r: DECK_OPEN.r });
    } else if (composing && leaving) {  // and ends as the handle
      shapes.push({ x: (COMPOSE_W - DECK_SHUT.w) / 2, y: DECK_TOP + (DECK_OPEN.h - DECK_SHUT.h) / 2, w: DECK_SHUT.w, h: DECK_SHUT.h, r: DECK_SHUT.r });
    } else if (composing) {
      of(pillRef.current, 26);  // first: the deck's glass becomes the pill, the preview's fades in
      of(previewRef.current, 18);
    } else if (talking) {
      of(previewRef.current, 18);
    } else if (deckOpen) {
      shapes.push({ x: (PET_W - DECK_OPEN.w) / 2, y: DECK_TOP, w: DECK_OPEN.w, h: DECK_OPEN.h, r: DECK_OPEN.r });
    } else if (working && phaseRef.current) {
      const w = phaseRef.current.offsetWidth + 26;
      shapes.push({ x: (PET_W - w) / 2, y: DECK_TOP + 10, w, h: 28, r: 14 });
    } else {
      shapes.push({ x: (PET_W - DECK_SHUT.w) / 2, y: DECK_TOP + (DECK_OPEN.h - DECK_SHUT.h) / 2, w: DECK_SHUT.w, h: DECK_SHUT.h, r: DECK_SHUT.r });
    }
    const key = JSON.stringify(shapes);
    const instant = widthJumped.current;  // the window just changed width: the glass moves with the page, not after it
    widthJumped.current = false;
    if (key !== glassSent.current) {
      glassSent.current = key;
      post({ type: "glass", shapes, ...(instant ? { instant: true } : {}) });
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
    window.clearTimeout(leaveTimer.current);
    window.clearTimeout(hoverTimer.current);
    setLeaving(false);
    setEntering(!reduced);
    setComposing(true);
    setHovered(false);
    post({ type: "chat", on: true });
    requestAnimationFrame(() => document.getElementById("pet-input")?.focus());
  }, [reduced]);
  const stopComposing = useCallback(() => {
    const done = () => {
      setLeaving(false);
      setComposing(false);  // what you were typing, and the thread, are still there when you open it again
    };
    window.clearTimeout(leaveTimer.current);
    if (reduced) { done(); return; }
    setEntering(false);
    setLeaving(true);
    leaveTimer.current = window.setTimeout(done, LEAVE_MS);
  }, [reduced]);
  const openChat = useCallback(() => {
    window.clearTimeout(closeTimer.current);
    setChatClosing(false);
    if (!chatting) fromPet.current = wasPet.current;
    setComposing(false);
    setChatting(true);
    post({ type: "chat", on: true });  // already open: take the keyboard again
    document.getElementById("composer-input")?.focus();
  }, [chatting]);
  const closeChat = useCallback(() => {
    const done = () => {
      setChatClosing(false);
      setChatting(false);
      if (fromPet.current) {  // back to the floating orb it came from, not tucked away
        setChoosingAt((at) => at || Date.now());
        return;
      }
      setChoosingAt(0);
      setDismissedSeq(seq);  // tucks away; anything new brings the orb back
    };
    window.clearTimeout(closeTimer.current);
    if (reduced || !inHelper()) { done(); return; }
    setChatClosing(true);
    closeTimer.current = window.setTimeout(done, CLOSE_MS);
  }, [seq, reduced]);
  useEffect(() => () => window.clearTimeout(closeTimer.current), []);
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
    } else if (message.type === "resized") {  // dragged by the corner grip; kept for next time
      const next = clampChat(message.width, message.height);
      setChatSize((was) => (was.w === next.w && was.h === next.h ? was : next));
      if (message.done) {
        try { localStorage.setItem(CHAT_SIZE_KEY, JSON.stringify(next)); } catch { /* no storage: this time only */ }
      }
    } else if (message.type === "hover") {
      hover(message.on);
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
  }), [app, chatting, composing, openChat, closeChat, stopComposing, talk, summonMode, hover]);
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
    return (
      <div className={`qc-frame ${glassChat ? "qc-frame--glass" : ""} ${chatClosing ? "qc-frame--closing" : ""}`}>
        <QuickChat onClose={closeChat} onOpenApp={openApp} onDragStart={() => post({ type: "dragStart" })} />
        {inHelper() && (
          <span className="qc-grip" role="presentation" title="Drag to resize"
            onPointerDown={(e) => { e.preventDefault(); post({ type: "resizeStart", minWidth: CHAT_MIN.w, minHeight: CHAT_MIN.h, maxWidth: CHAT_MAX.w, maxHeight: CHAT_MAX.h }); }} />
        )}
      </div>
    );
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
    const lines = thread.slice(-THREAD_LINES);
    const asked = lines.length > 0 || Boolean(streaming?.text);
    const hint = listening ? "Listening…" : working ? orb.label : composing ? "Open the chat" : "Continue here";
    const send = () => {
      const text = draft.trim();
      if (!text) {
        setNudged(true);
        window.setTimeout(() => setNudged(false), 320);
        return;
      }
      startThread(conversation.length);
      setDraft("");
      void app.send(text, "text", true);  // answered on screen: the pill does not speak
    };
    const preview = (onClick: () => void, label: string) => (
      <button type="button" className="pet__preview" ref={previewRef} onClick={onClick} aria-label={label}>
        <span className={`pet__thread ${lines.length >= 3 || lines.reduce((n, m) => n + m.text.length, 0) > 220 ? "pet__thread--long" : ""}`}>
          {lines.map((m) => <span key={m.id} className={`pet__line pet__line--${m.role}`}>{m.text}</span>)}
          {streaming?.text ? <span className="pet__line pet__line--assistant">{streaming.text}</span>
            : working && lines.at(-1)?.role !== "assistant" ? <span className="pet__line pet__line--assistant pet__line--wait">{workingLabel([], false)}…</span> : null}
        </span>
        <span className="pet__preview-hint">{hint}</span>
      </button>
    );
    return (
      <div className={`pet ${composing || talking ? "pet--compose" : ""} ${talking ? "pet--talking" : ""} ${entering ? "pet--entering" : ""} ${leaving ? "pet--leaving" : ""} ${deckOpen ? "pet--open" : ""} ${inHelper() ? "pet--native" : ""}`} ref={box}
        onPointerEnter={() => hover(true)} onPointerLeave={() => hover(false)}>
        <div className="pet__orb" aria-hidden="false">
          <Orb state={orb.state} label={orb.label} size={PET_ORB} detail="low" reducedMotion={reduced} level={orb.level ?? (music ? musicSource : null)}
            agents={orb.agents} moves={moves} beats={beats} vivid scale={SPHERE} idle={idle} />
          <span className="pet__hit" onPointerDown={onDown} onPointerUp={onUp} title="Drag to move · click to talk" />
        </div>
        {composing ? (
          <>
            {asked && preview(openChat, "Open the whole chat")}
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
        ) : talking ? preview(compose, "Continue here") : (
          <div className="pet__deck" role="group" aria-label="Talk to XENIO" style={{ top: DECK_TOP }}
            onFocus={() => hover(true)} onBlur={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node)) hover(false); }}>
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
