import { MessageSquarePlus, Minus, PanelRightClose, Pause, Play, Plus } from "lucide-react";
import { useLayoutEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent, type RefObject } from "react";
import { Composer, PendingApprovals, Transcript } from "../components/Conversation";
import { Button, Empty, IconButton, Loaded, Section, StatusOf, Time } from "../components/ui";
import { useOrbState } from "../components/useOrbState";
import { arrive, useGsap, useReducedMotion } from "../motion/motion";
import { Orb } from "../orb/Orb";
import { useApp, useAppState } from "../state/context";
import { ORB_STEPS, SIDE_MAX, SIDE_MIN, setLayout, toggleFolded, useLayout } from "../state/layout";
import { href } from "../state/router";
import { useResource } from "../state/useResource";
import { activityLine } from "./activityText";

function useFold(id: string) {
  const folded = useLayout().folded.includes(id);
  return { folded, onToggle: () => toggleFolded(id) };
}

const ACTIVE = ["planning", "planned", "running", "verifying", "reviewing"];

function RunningNow() {
  const { app } = useApp();
  const fold = useFold("home-running");
  const runs = useResource((s) => app.api.runs(s), ["runs"]);
  const active = runs.data?.available ? runs.data.runs.filter((r) => r.live && ACTIVE.includes(r.status)) : [];
  const summary = !runs.data ? null : !runs.data.available ? "switched off" : active.length ? `${active.length} running` : "nothing running";
  return (
    <Section title="Running now" id="home-running" fold={{ ...fold, summary }}
      action={<a className="link" href={href("orchestration")}>All runs</a>}>
      <Loaded resource={runs} what="engineering runs">
        {(data) => {
          if (!data.available) return <Empty>{data.reason ?? "The engineering team is switched off."}</Empty>;
          if (!active.length) return <Empty>Nothing is running. Ask XENIO to build, fix, review or explain a project.</Empty>;
          return (
            <ul className="rows">
              {active.map((r) => {
                const total = Object.values(r.tasks).reduce((a, b) => a + (b ?? 0), 0);
                const done = r.tasks.succeeded ?? 0;
                return (
                  <li key={r.id}>
                    <a className="row-link" href={href("orchestration", r.id)}>
                      <span className="row-main">{r.request}</span>
                      <span className="row-side"><StatusOf value={r.status} />
                        <span className="meta tabular">{total ? `${done} of ${total} tasks done` : "no plan yet"}
                          {r.running.length ? ` · ${r.running.length} working` : ""}</span>
                      </span>
                    </a>
                  </li>
                );
              })}
            </ul>
          );
        }}
      </Loaded>
    </Section>
  );
}

function NextChecks() {
  const { app } = useApp();
  const fold = useFold("home-schedules");
  const jobs = useResource((s) => app.api.schedules(s), ["schedules"]);
  const next = (jobs.data?.jobs ?? []).filter((j) => j.enabled && j.next_run)
    .sort((a, b) => String(a.next_run).localeCompare(String(b.next_run))).slice(0, 3);
  const summary = !jobs.data ? null : next[0] ? <>{next[0].name} <Time iso={next[0].next_run} /></> : "none scheduled";
  return (
    <Section title="Next scheduled checks" id="home-schedules" fold={{ ...fold, summary }}
      action={<a className="link" href={href("schedules")}>Schedules</a>}>
      <Loaded resource={jobs} what="scheduled checks">
        {() => next.length ? (
          <ul className="rows">
            {next.map((j) => <li key={j.id} className="row"><span className="row-main">{j.name}</span><Time iso={j.next_run} /></li>)}
          </ul>
        ) : <Empty>No scheduled checks. Ask XENIO, for example: "check my inbox every morning at 8".</Empty>}
      </Loaded>
    </Section>
  );
}

function RecentActivity() {
  const { app } = useApp();
  const fold = useFold("home-activity");
  const activity = useResource((s) => app.api.activity(6, s), ["activity"]);
  const latest = activity.data?.events[0];
  const summary = !activity.data ? null : latest ? activityLine(latest) : "nothing yet";
  return (
    <Section title="Recent activity" id="home-activity" fold={{ ...fold, summary }}
      action={<a className="link" href={href("activity")}>Activity</a>}>
      <Loaded resource={activity} what="activity">
        {(data) => data.events.length ? (
          <ul className="rows">
            {data.events.slice(0, 5).map((e) => <li key={e.id} className="row"><span className="row-main">{activityLine(e)}</span><Time iso={e.at} /></li>)}
          </ul>
        ) : <Empty>No activity yet.</Empty>}
      </Loaded>
    </Section>
  );
}

function SystemSummary() {
  const { app } = useApp();
  const fold = useFold("home-system");
  const health = useResource((s) => app.api.health(false, s), ["health"]);
  const checks = health.data?.checks ?? [];
  const problems = checks.filter((c) => c.status === "FAIL" || c.status === "WARN");
  const summary = !health.data ? null : `${checks.length - problems.length} of ${checks.length} parts working`;
  return (
    <Section title="System" id="home-system" fold={{ ...fold, summary }} action={<a className="link" href={href("system")}>Details</a>}>
      <Loaded resource={health} what="system checks">
        {() => (
          <div className="system-summary">
            <p className="tabular">{summary}</p>
            {[...problems].sort((a) => (a.status === "FAIL" ? -1 : 1)).slice(0, 3).map((c) => (
              <p key={c.name} className="row"><StatusOf value={c.status} /> <span className="row-main">{c.name}: {c.detail}</span></p>
            ))}
          </div>
        )}
      </Loaded>
    </Section>
  );
}

/**
 * The orb takes the room it has: beside the conversation when Home is wide enough, above it otherwise. It grows when
 * the side panel or the sidebar is put away; the viewer's size choice (ORB_STEPS) is a share of that room.
 */
function useStage(ref: RefObject<HTMLDivElement | null>, step: number): { wide: boolean; size: number } {
  const [stage, setStage] = useState({ wide: true, size: 300 });
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => {
      const w = el.clientWidth || window.innerWidth;
      const h = el.clientHeight || window.innerHeight - 56;
      const wide = w >= 760 && h >= 460;
      const room = wide ? Math.min(w * 0.44 - 40, h - 150) : Math.min(w - 32, h * 0.4);
      const size = Math.round(Math.max(120, room * (ORB_STEPS[step] ?? 1)));
      setStage((s) => (s.wide === wide && s.size === size ? s : { wide, size }));
    };
    measure();
    const observer = typeof ResizeObserver === "function" ? new ResizeObserver(measure) : null;
    observer?.observe(el);
    window.addEventListener("resize", measure);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [ref, step]);
  return stage;
}

/** Drag (or arrow keys) to resize the side panel. */
function Splitter({ width }: { width: number }) {
  const drag = useRef<{ x: number; width: number } | null>(null);
  const onDown = (e: PointerEvent<HTMLDivElement>) => {
    drag.current = { x: e.clientX, width };
    e.currentTarget.setPointerCapture(e.pointerId);
  };
  const onMove = (e: PointerEvent<HTMLDivElement>) => {
    if (drag.current) setLayout({ sideWidth: drag.current.width + (drag.current.x - e.clientX) });
  };
  const onUp = () => { drag.current = null; };
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const step = e.shiftKey ? 64 : 16;
    if (e.key === "ArrowLeft") setLayout({ sideWidth: width + step });
    else if (e.key === "ArrowRight") setLayout({ sideWidth: width - step });
    else if (e.key === "Home") setLayout({ sideWidth: SIDE_MAX });
    else if (e.key === "End") setLayout({ sideWidth: SIDE_MIN });
    else return;
    e.preventDefault();
  };
  return (
    <div className="splitter" role="separator" aria-orientation="vertical" aria-label="Resize the side panel"
      aria-valuenow={width} aria-valuemin={SIDE_MIN} aria-valuemax={SIDE_MAX} tabIndex={0}
      onPointerDown={onDown} onPointerMove={onMove} onPointerUp={onUp} onPointerCancel={onUp} onKeyDown={onKey}
      onDoubleClick={() => setLayout({ sideWidth: 340 })} title="Drag to resize · double-click to reset" />
  );
}

export function HomePage() {
  const { app } = useApp();
  const orb = useOrbState();
  const reduced = useReducedMotion();
  const assistant = useAppState((s) => s.assistant);
  const detail = useAppState((s) => s.settings?.orb_detail ?? "auto");
  const approvals = useAppState((s) => s.approvals.length);
  const wake = useAppState((s) => (s.snapshot?.voice.wake ? s.snapshot.voice.wake_phrase ?? null : null));
  const layout = useLayout();
  const root = useRef<HTMLDivElement>(null);
  const main = useRef<HTMLDivElement>(null);
  const stage = useStage(main, layout.orbStep);
  useGsap(root, (r) => { arrive(".home-side .section", r, 0.06); }, [layout.side]);
  const background = assistant?.background;
  const paused = Boolean(assistant?.paused);

  const newConversation = () => void app.api.newConversation().catch((e: Error) =>
    app.store.notify({ tone: "danger", title: "Could not start a new conversation", message: e.message }));

  return (
    <div className={`home ${layout.side ? "" : "home--focus"}`} ref={root}
      style={{ "--side-w": `${layout.sideWidth}px` } as CSSProperties}>
      <div className={`home-main ${stage.wide ? "home-main--wide" : ""}`} ref={main}>
        <section className="home-hero" aria-labelledby="home-state">
          <div className="home-orb">
            <Orb state={orb.state} label={orb.label} size={stage.size} detail={detail} reducedMotion={reduced} level={orb.level}
              agents={orb.agents} interactive />
          </div>
          <h1 className="home-state" id="home-state" aria-live="polite">{orb.label}</h1>
          <p className="home-sentence">{orb.sentence}</p>
          {background?.state && orb.state !== background.state && (
            <p className="meta home-background">
              In the background: {background.agents ? `${background.agents} agent${background.agents > 1 ? "s" : ""} working` : "the team is planning"}
              {" "}on {background.runs} run{background.runs > 1 ? "s" : ""}. <a className="link" href={href("orchestration")}>See the work</a>
            </p>
          )}
          {wake && orb.state === "IDLE" && <p className="meta home-wake">Say “{wake.replace(/\b\w/g, (c) => c.toUpperCase())}” to talk</p>}
          <div className="orb-size" role="group" aria-label="Orb size">
            <IconButton label="Smaller orb" icon={Minus} disabled={layout.orbStep <= 0}
              onClick={() => setLayout((l) => ({ orbStep: l.orbStep - 1 }))} />
            <IconButton label="Bigger orb" icon={Plus} disabled={layout.orbStep >= ORB_STEPS.length - 1}
              onClick={() => setLayout((l) => ({ orbStep: l.orbStep + 1 }))} />
          </div>
          {assistant?.state === "ERROR" && <Button variant="quiet" onClick={() => void app.api.dismissError()}>Dismiss the error</Button>}
        </section>
        <section className="home-chat" aria-label="Conversation">
          <div className="home-chat__head">
            <h2 className="home-chat__title">Conversation</h2>
            <IconButton label={paused ? "Resume XENIO" : "Pause XENIO"} icon={paused ? Play : Pause}
              onClick={() => void app.api.setPaused(!paused)} />
            <IconButton label="New conversation" icon={MessageSquarePlus} onClick={newConversation} />
          </div>
          <div className="home-chat__scroll">
            {approvals > 0 && <PendingApprovals compact />}
            <Transcript limit={40} />
          </div>
          <Composer autoFocus />
        </section>
      </div>
      {layout.side && (
        <>
          <Splitter width={layout.sideWidth} />
          <aside className="home-side" aria-label="At a glance">
            <div className="home-side__head">
              <p className="home-side__title">At a glance</p>
              <IconButton label="Hide the side panel" icon={PanelRightClose} onClick={() => setLayout({ side: false })} />
            </div>
            <RunningNow />
            <NextChecks />
            <RecentActivity />
            <SystemSummary />
          </aside>
        </>
      )}
    </div>
  );
}
