import { gsap } from "gsap";
import {
  Activity, Bot, CalendarClock, CheckSquare, Cpu, Database, GitBranch, Home, Maximize2, MessageSquare, PanelLeftClose,
  PanelLeftOpen, PanelRightClose, PanelRightOpen, PlugZap, Settings, ShieldCheck, Wrench, X, type LucideIcon,
} from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { MOTION, useReducedMotion } from "../motion/motion";
import { Orb } from "../orb/Orb";
import { useApp, useAppState } from "../state/context";
import { setLayout, useLayout } from "../state/layout";
import { href, PAGES, type PageId } from "../state/router";
import { Composer, PendingApprovals, Transcript } from "./Conversation";
import { IconButton, Status, Time } from "./ui";
import { useOrbState } from "./useOrbState";

const ICONS: Record<PageId, LucideIcon> = {
  home: Home, agents: Bot, orchestration: GitBranch, tasks: CheckSquare, schedules: CalendarClock, tools: Wrench,
  memory: Database, activity: Activity, approvals: ShieldCheck, integrations: PlugZap, system: Cpu, settings: Settings,
};

function ConnectionStatus() {
  const stream = useAppState((s) => s.stream);
  const assistant = useAppState((s) => s.assistant);
  const demo = useAppState((s) => s.snapshot?.demo ?? false);
  if (demo) return <Status tone="attention">Demo data</Status>;
  if (stream === "unauthorized") return <Status tone="danger">Not signed in</Status>;
  if (stream === "retrying" || stream === "closed") return <Status tone="danger">Reconnecting to XENIO</Status>;
  if (!assistant || stream === "connecting") return <Status tone="running">Connecting</Status>;
  if (assistant.connection !== "connected") return <Status tone="danger">Hermes {assistant.connection}</Status>;
  return <Status tone="ok">Connected</Status>;
}

/** The floating orb's quick chat, opened from here (it floats over every app, this window included). */
function QuickChatButton() {
  const { app } = useApp();
  const running = useAppState((s) => s.snapshot?.overlay?.running === true);
  if (!running) return null;
  return (
    <IconButton label="Quick chat (the floating orb's chat)" icon={MessageSquare}
      onClick={() => void app.api.summonOrb("chat").catch((e: Error) =>
        app.store.notify({ tone: "danger", title: "The quick chat did not open", message: e.message }))} />
  );
}

/** While the live stream is down, say so - and how old the shown state is - instead of passing it off as current. */
function StaleBanner() {
  const stream = useAppState((s) => s.stream);
  const last = useAppState((s) => s.lastEventAt);
  if (stream !== "retrying" && stream !== "closed") return null;
  return (
    <p className="banner banner--attention stale-banner" role="status">
      Live updates paused: reconnecting to XENIO. What you see was current {last ? <Time iso={last} /> : "when the page loaded"};
      nothing shown here changes until XENIO answers again.
    </p>
  );
}

function Sidebar({ page, rail }: { page: PageId; rail: boolean }) {
  const approvals = useAppState((s) => s.approvals.length);
  const name = useAppState((s) => s.snapshot?.name ?? "Xenio");
  return (
    <nav className="sidebar" aria-label="Mission Control">
      <a className="wordmark" href={href("home")} aria-label={name.toUpperCase()}>
        <span className="wordmark__full" aria-hidden={rail}>{name.toUpperCase()}</span>
        <span className="wordmark__short" aria-hidden="true">{name.slice(0, 1).toUpperCase()}</span>
      </a>
      <ul>
        {PAGES.map((p) => {
          const Icon = ICONS[p.id];
          const current = p.id === page;
          return (
            <li key={p.id}>
              <a href={href(p.id)} className={`nav-link ${current ? "nav-link--current" : ""}`} aria-current={current ? "page" : undefined}
                title={rail ? p.label : undefined}>
                <Icon size={17} aria-hidden="true" />
                <span className="nav-link__label">{p.label}</span>
                {p.id === "approvals" && approvals > 0 && (
                  <span className="nav-count" aria-label={`${approvals} waiting`}>{approvals}</span>
                )}
              </a>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}

function TopBar({ page, onMenu }: { page: PageId; onMenu: () => void }) {
  const label = PAGES.find((p) => p.id === page)?.label ?? "";
  const layout = useLayout();
  const rail = layout.sidebar === "rail";
  return (
    <header className="topbar">
      <button type="button" className="menu-btn" onClick={onMenu} aria-label="Show navigation">Menu</button>
      <IconButton className="sidebar-toggle" label={rail ? "Expand the sidebar" : "Collapse the sidebar"}
        icon={rail ? PanelLeftOpen : PanelLeftClose} aria-pressed={rail}
        onClick={() => setLayout({ sidebar: rail ? "open" : "rail" })} />
      <p className="topbar__where" aria-hidden="true">{label}</p>
      <div className="topbar__right">
        <ConnectionStatus />
        <QuickChatButton />
        {page === "home" && (
          <IconButton label={layout.side ? "Hide the side panel (focus on the conversation)" : "Show the side panel"}
            icon={layout.side ? PanelRightClose : PanelRightOpen} aria-pressed={!layout.side}
            onClick={() => setLayout({ side: !layout.side })} />
        )}
      </div>
    </header>
  );
}

function Notices() {
  const { store } = useApp();
  const notices = useAppState((s) => s.notices);
  useEffect(() => {
    if (!notices.length) return;
    const latest = notices[notices.length - 1]!;
    const timer = setTimeout(() => store.dismiss(latest.id), latest.tone === "danger" || latest.tone === "attention" ? 12_000 : 7000);
    return () => clearTimeout(timer);
  }, [notices, store]);
  return (
    <div className="notices" role="region" aria-label="Notifications" aria-live="polite">
      {notices.map((n) => (
        <div key={n.id} className={`notice notice--${n.tone}`}>
          <div>
            <p className="notice__title">{n.title}</p>
            {n.message && <p className="notice__text">{n.message}</p>}
          </div>
          <IconButton label="Dismiss" icon={X} onClick={() => store.dismiss(n.id)} />
        </div>
      ))}
    </div>
  );
}

/** The floating orb and its panel: opens from the orb and closes back into it (same path both ways). */
function FloatingAssistant() {
  const [open, setOpen] = useState(false);
  const orb = useOrbState();
  const reduced = useReducedMotion();
  const detail = useAppState((s) => s.settings?.orb_detail ?? "auto");
  const approvals = useAppState((s) => s.approvals.length);
  const panel = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const timeline = useRef<gsap.core.Timeline | null>(null);

  useEffect(() => {
    const el = panel.current;
    if (!el) return;
    timeline.current?.kill();
    if (open) {
      el.hidden = false;
      timeline.current = reduced
        ? gsap.timeline().fromTo(el, { opacity: 0 }, { opacity: 1, duration: MOTION.fast })
        : gsap.timeline().fromTo(el, { opacity: 0, scale: 0.9, filter: "blur(6px)" },
          { opacity: 1, scale: 1, filter: "blur(0px)", duration: MOTION.base, ease: MOTION.ease, clearProps: "filter" });
      el.querySelector<HTMLTextAreaElement>("textarea")?.focus();
    } else if (!el.hidden) {
      // starts from wherever the opening animation is now (interruptible), and ends hidden
      timeline.current = gsap.timeline({ onComplete: () => { el.hidden = true; } }).to(el, reduced
        ? { opacity: 0, duration: MOTION.fast }
        : { opacity: 0, scale: 0.92, duration: MOTION.fast, ease: MOTION.easeIn });
    }
    return () => { timeline.current?.kill(); };
  }, [open, reduced]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { setOpen(false); button.current?.focus(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  return (
    <div className="floating">
      <div ref={panel} className="assistant-panel" role="dialog" aria-modal="false" aria-labelledby="assistant-title" hidden>
        <div className="assistant-panel__head">
          <Orb state={orb.state} label={orb.label} size={44} detail="low" reducedMotion={reduced} level={orb.level} agents={orb.agents} />
          <div className="assistant-panel__who">
            <h2 id="assistant-title">XENIO</h2>
            <p className="meta" aria-live="polite">{orb.sentence}</p>
          </div>
          <a className="icon-btn" href={href("home")} onClick={() => setOpen(false)} aria-label="Open Mission Control" title="Open Mission Control">
            <Maximize2 size={18} aria-hidden="true" />
          </a>
          <IconButton label="Close" icon={X} onClick={() => { setOpen(false); button.current?.focus(); }} />
        </div>
        <div className="assistant-panel__body">
          <PendingApprovals compact />
          <Transcript limit={30} />
        </div>
        <Composer />
      </div>
      <button ref={button} type="button" className={`orb-button ${approvals ? "orb-button--attention" : ""}`}
        onClick={() => setOpen((o) => !o)} aria-expanded={open} aria-label={`${open ? "Close" : "Open"} XENIO: ${orb.label}`}>
        <Orb state={orb.state} label={orb.label} size={58} detail={detail === "high" ? "auto" : "low"} reducedMotion={reduced}
          level={orb.level} agents={orb.agents} />
      </button>
    </div>
  );
}

export function Shell({ page, children }: { page: PageId; children: ReactNode }) {
  const [navOpen, setNavOpen] = useState(false);
  const demo = useAppState((s) => s.snapshot?.demo ?? false);
  const rail = useLayout().sidebar === "rail";
  useEffect(() => { setNavOpen(false); }, [page]);
  return (
    <div className={`shell ${navOpen ? "shell--nav-open" : ""} ${rail ? "shell--rail" : ""}`}>
      <a className="skip-link" href="#main">Skip to content</a>
      {demo && <p className="demo-banner" role="note">Demo data: this page is not connected to XENIO. Nothing shown here is real.</p>}
      <Sidebar page={page} rail={rail} />
      <div className="main-col">
        <TopBar page={page} onMenu={() => setNavOpen((o) => !o)} />
        <StaleBanner />
        <main id="main" className={`content ${page === "home" ? "content--home" : ""}`} tabIndex={-1}>{children}</main>
      </div>
      {page !== "home" && <FloatingAssistant />}
      <Notices />
    </div>
  );
}
