import { RefreshCw } from "lucide-react";
import { useState } from "react";
import type { HealthCheck, UiSettings } from "../api/types";
import { Button, Empty, ErrorNote, Loaded, PageHeader, Section, Status, StatusOf, Time } from "../components/ui";
import { useApp, useAppState } from "../state/context";
import { useResource } from "../state/useResource";

const INTEGRATIONS: { name: string; checks: string[]; what: string; needs: string }[] = [
  { name: "Hermes Agent", checks: ["Hermes", "Model"], what: "XENIO's reasoning engine and its model", needs: "Hermes installed; a model provider" },
  { name: "Safety guard", checks: ["Safety"], what: "Checks every tool call against XENIO's rules", needs: "python -m app.main --install-guard" },
  { name: "Speech recognition", checks: ["STT"], what: "Turns what you say into text", needs: "STT_API_KEY and STT_API_URL in XENIO's .env" },
  { name: "Speech output", checks: ["TTS"], what: "XENIO's voice", needs: "TTS_API_KEY and TTS_API_URL in XENIO's .env" },
  { name: "Google Calendar and Gmail", checks: ["Google"], what: "Your calendar and read-only mail", needs: "python -m app.main --connect-google" },
  { name: "Email", checks: ["Email"], what: "Searching and reading mail", needs: "Google, or a mailbox set up with himalaya" },
  { name: "GitHub", checks: ["GitHub"], what: "Issues, pull requests, CI", needs: "gh signed in, or XENIO_GITHUB_USER" },
  { name: "Web and browser", checks: ["Web"], what: "Search, reading pages, opening pages in your browser", needs: "Nothing extra" },
  { name: "Screen vision", checks: ["Vision"], what: "Looking at your screen when you ask", needs: "Screen & System Audio Recording permission" },
  { name: "Desktop control", checks: ["Desktop"], what: "Opening apps, windows, keyboard and mouse", needs: "Accessibility permission (and Automation for Terminal, Spotify)" },
  { name: "Memory", checks: ["Memory"], what: "What XENIO remembers", needs: "Nothing extra" },
  { name: "Scheduled checks", checks: ["Schedules"], what: "Checks that run on a schedule", needs: "XENIO running, or a Hermes gateway" },
  { name: "Engineering team", checks: ["Engineering", "Engineer"], what: "Specialist agents for your repositories", needs: "git; XENIO_ENGINEERING not switched off" },
];

function worstOf(checks: HealthCheck[]): HealthCheck | undefined {
  return checks.find((c) => c.status === "FAIL") ?? checks.find((c) => c.status === "WARN") ?? checks[0];
}

export function IntegrationsPage() {
  const { app } = useApp();
  const [force, setForce] = useState(0);
  const health = useResource((s) => app.api.health(force > 0, s), ["health"], [force]);
  return (
    <>
      <PageHeader title="Integrations" intro="The services XENIO uses and whether each works right now. No key or token is ever shown here."
        actions={<Button variant="quiet" onClick={() => setForce((f) => f + 1)} disabled={health.refreshing}><RefreshCw size={14} aria-hidden="true" /> Check again</Button>} />
      <Loaded resource={health} what="integrations">
        {(data) => {
          const by = Object.fromEntries(data.checks.map((c) => [c.name, c]));
          return (
            <>
              <p className="meta">Checked <Time iso={data.checked_at} />.</p>
              <ul className="integrations">
                {INTEGRATIONS.map((it) => {
                  const checks = it.checks.map((n) => by[n]).filter((c): c is HealthCheck => Boolean(c));
                  const worst = worstOf(checks);
                  return (
                    <li key={it.name} className="integration">
                      <div className="integration__head">
                        <h2 className="integration__name">{it.name}</h2>
                        {worst ? <StatusOf value={worst.status} /> : <Status tone="neutral">Not checked</Status>}
                      </div>
                      <p>{it.what}</p>
                      {checks.map((c) => <p key={c.name} className="meta">{c.name}: {c.detail}</p>)}
                      {worst && worst.status !== "OK" && <p className="meta">Needs: {it.needs}</p>}
                    </li>
                  );
                })}
              </ul>
            </>
          );
        }}
      </Loaded>
    </>
  );
}

export function SystemPage() {
  const { app } = useApp();
  const [force, setForce] = useState(0);
  const health = useResource((s) => app.api.health(force > 0, s), ["health"], [force]);
  const assistant = useAppState((s) => s.assistant);
  const snapshot = useAppState((s) => s.snapshot);
  const stream = useAppState((s) => s.stream);
  const lastEvent = useAppState((s) => s.lastEventAt);
  return (
    <>
      <PageHeader title="System" intro="XENIO's own checks (the same as python -m app.main --health). Only what XENIO really measures is shown."
        actions={<Button variant="quiet" onClick={() => setForce((f) => f + 1)} disabled={health.refreshing}><RefreshCw size={14} aria-hidden="true" /> Run the checks</Button>} />
      <Section title="Right now" id="sys-now">
        <dl className="facts">
          <dt>Live updates</dt><dd>{stream === "open" ? <Status tone="ok">Connected</Status> : <Status tone="danger">{stream}</Status>}{lastEvent && <> · last event <Time iso={lastEvent} /></>}</dd>
          <dt>Hermes</dt><dd>{assistant?.connection ?? "unknown"}{snapshot?.hermes.name ? ` · ${snapshot.hermes.name} ${snapshot.hermes.version ?? ""}` : ""}</dd>
          <dt>Assistant</dt><dd>{assistant ? assistant.state.toLowerCase().replace(/_/g, " ") : "unknown"}{assistant?.paused ? " (paused)" : ""}</dd>
          <dt>Engineering</dt><dd>{assistant?.background.runs ? `${assistant.background.runs} active run(s), ${assistant.background.agents} agent(s) working` : "nothing running"}</dd>
          <dt>Platform</dt><dd>{snapshot?.platform === "darwin" ? "macOS" : snapshot?.platform ?? "unknown"}</dd>
          <dt>CPU, memory, uptime</dt><dd className="meta">Not measured by XENIO</dd>
        </dl>
      </Section>
      <Section title="Checks" id="sys-checks">
        <Loaded resource={health} what="system checks">
          {(data) => data.checks.length ? (
            <table className="table">
              <caption className="sr-only">System checks</caption>
              <thead><tr><th scope="col">Part</th><th scope="col">Status</th><th scope="col">Detail</th></tr></thead>
              <tbody>{data.checks.map((c) => <tr key={c.name}><td>{c.name}</td><td><StatusOf value={c.status} /></td><td>{c.detail}</td></tr>)}</tbody>
            </table>
          ) : <Empty>No checks ran.</Empty>}
        </Loaded>
      </Section>
    </>
  );
}

export function SettingsPage() {
  const { app, store } = useApp();
  const settings = useAppState((s) => s.settings);
  const tts = useAppState((s) => s.snapshot?.voice.tts ?? false);
  const voice = useAppState((s) => s.snapshot?.voice);
  const assistant = useAppState((s) => s.assistant);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const save = async (changes: Partial<UiSettings>) => {
    setError(null);
    setSaved(false);
    try {
      store.set({ settings: await app.api.saveSettings(changes) });
      setSaved(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };
  if (!settings) return <><PageHeader title="Settings" /><ErrorNote message="Settings could not be loaded from XENIO." /></>;
  return (
    <>
      <PageHeader title="Settings" intro="Saved by XENIO in its data folder on this Mac." />
      {error && <ErrorNote message={error} />}
      <p className="sr-only" role="status">{saved ? "Saved" : ""}</p>
      <Section title="Motion" id="set-motion">
        <fieldset className="choices">
          <legend className="sr-only">Motion</legend>
          {([["system", "Follow macOS (Reduce motion)"], ["full", "Full motion"], ["reduced", "Reduced: fades instead of movement, a still orb"]] as const).map(([value, label]) => (
            <label key={value} className="choice"><input type="radio" name="motion" checked={settings.motion === value}
              onChange={() => void save({ motion: value })} /> {label}</label>
          ))}
        </fieldset>
      </Section>
      <Section title="Voice" id="set-voice">
        <p className="meta set-voice-engine">
          {voice?.engine ? <><StatusOf value="OK" /> XENIO listens and speaks through this Mac's microphone and speaker, like --voice.</>
            : <><StatusOf value="WARN" /> XENIO's microphone is not in use{voice?.problem ? `: ${voice.problem}` : ""}. The page records in the browser instead.</>}
        </p>
        <label className="choice"><input type="checkbox" checked={settings.wake_word ?? true} disabled={!voice?.engine}
          onChange={(e) => void save({ wake_word: e.target.checked })} /> Listen for “{(voice?.wake_phrase ?? "hey xenio").replace(/\b\w/g, (c) => c.toUpperCase())}”
          {voice?.engine && !voice.wake && settings.wake_word !== false && <span className="meta"> (the wake-word model is not available; use the microphone button)</span>}</label>
        <label className="choice"><input type="checkbox" checked={settings.speak_replies} disabled={!tts}
          onChange={(e) => void save({ speak_replies: e.target.checked })} /> Speak every reply aloud {!tts && <span className="meta">(speech output is not set up)</span>}</label>
        <p className="meta">Replies to what you say are always spoken when speech output works; this adds typed requests.</p>
      </Section>
      <Section title="Orb" id="set-orb">
        <fieldset className="choices">
          <legend className="sr-only">Orb detail</legend>
          {([["auto", "Automatic detail"], ["low", "Low detail (lighter on the battery)"], ["high", "High detail"]] as const).map(([value, label]) => (
            <label key={value} className="choice"><input type="radio" name="orb" checked={settings.orb_detail === value}
              onChange={() => void save({ orb_detail: value })} /> {label}</label>
          ))}
        </fieldset>
      </Section>
      <Section title="Notifications" id="set-notes">
        <label className="choice"><input type="checkbox" checked={settings.notifications} onChange={(e) => void save({ notifications: e.target.checked })} /> Show milestones and scheduled-check results as they arrive</label>
      </Section>
      <Section title="XENIO" id="set-xenio">
        <div className="button-row">
          <Button onClick={() => void app.api.setPaused(!assistant?.paused)}>{assistant?.paused ? "Resume XENIO" : "Pause XENIO"}</Button>
          <Button onClick={() => void app.api.newConversation().then(() => store.notify({ tone: "ok", title: "New conversation", message: "XENIO starts fresh; its memory stays." }),
            (e: Error) => setError(e.message))}>Start a new conversation</Button>
        </div>
        <p className="meta">Privacy: this interface runs only on this Mac (127.0.0.1). What you type or say is not kept in the activity log; keys and tokens never reach this page.</p>
      </Section>
    </>
  );
}
