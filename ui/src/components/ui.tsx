import {
  AlertTriangle, CheckCircle2, ChevronDown, CircleDashed, CircleSlash, Clock3, Loader2, Lock, PauseCircle, RefreshCw, WifiOff,
  XCircle, type LucideIcon,
} from "lucide-react";
import type { ButtonHTMLAttributes, ComponentProps, ReactNode } from "react";

export type Tone = "ok" | "attention" | "danger" | "running" | "neutral";

const TONE_ICON: Record<Tone, LucideIcon> = {
  ok: CheckCircle2, attention: AlertTriangle, danger: XCircle, running: Loader2, neutral: CircleDashed,
};

/** Status is never colour alone: an icon and a word always come with it. */
export function Status({ tone, children, icon }: { tone: Tone; children: ReactNode; icon?: LucideIcon }) {
  const Icon = icon ?? TONE_ICON[tone];
  return (
    <span className={`status status--${tone}`}>
      <Icon aria-hidden="true" size={14} className={tone === "running" && !icon ? "spin-when-motion" : undefined} />
      <span>{children}</span>
    </span>
  );
}

const TASK_TONES: Record<string, [Tone, string, LucideIcon?]> = {
  pending: ["neutral", "Waiting", Clock3], ready: ["neutral", "Ready to start", Clock3], running: ["running", "Running"],
  blocked: ["attention", "Blocked", Lock], succeeded: ["ok", "Done"], failed: ["danger", "Failed"],
  cancelled: ["neutral", "Cancelled", CircleSlash], planning: ["running", "Planning"],
  planned: ["attention", "Waiting for your go-ahead", PauseCircle], verifying: ["running", "Verifying"],
  reviewing: ["running", "Reviewing"], done: ["ok", "Done"], in_progress: ["running", "In progress"],
  approved: ["ok", "Approved"], denied: ["neutral", "Declined", CircleSlash], expired: ["neutral", "Expired, not approved", Clock3],
  OK: ["ok", "Working"], WARN: ["attention", "Needs attention"], FAIL: ["danger", "Not working"],
  idle: ["neutral", "Idle", CircleDashed], working: ["running", "Working"], disabled: ["neutral", "Off", CircleSlash],
};

export function StatusOf({ value }: { value: string }) {
  const [tone, label, icon] = TASK_TONES[value] ?? (["neutral", value.replace(/_/g, " ")] as [Tone, string]);
  return <Status tone={tone} icon={icon}>{label}</Status>;
}

export function Button({ variant = "secondary", children, className, ...rest }:
  ComponentProps<"button"> & { variant?: "primary" | "secondary" | "quiet" | "danger" }) {
  return <button type="button" className={`btn btn--${variant} ${className ?? ""}`} {...rest}>{children}</button>;
}

export function IconButton({ label, icon: Icon, className, ...rest }:
  ButtonHTMLAttributes<HTMLButtonElement> & { label: string; icon: LucideIcon }) {
  return (
    <button type="button" className={`icon-btn ${className ?? ""}`} aria-label={label} title={label} {...rest}>
      <Icon aria-hidden="true" size={18} />
    </button>
  );
}

export function PageHeader({ title, intro, actions }: { title: string; intro?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="page-header">
      <div>
        <h1 className="page-title">{title}</h1>
        {intro && <p className="page-intro">{intro}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  );
}

export interface Fold {
  folded: boolean;
  onToggle: () => void;
  /** one line shown in the heading while the section is minimised */
  summary?: ReactNode;
}

export function Section({ title, action, children, id, fold }: {
  title: string; action?: ReactNode; children: ReactNode; id?: string; fold?: Fold;
}) {
  const head = id ? `${id}-h` : undefined;
  if (!fold) {
    return (
      <section className="section" aria-labelledby={head}>
        <div className="section__head">
          <h2 className="section__title" id={head}>{title}</h2>
          {action}
        </div>
        {children}
      </section>
    );
  }
  return (
    <section className={`section section--fold ${fold.folded ? "section--folded" : ""}`} aria-labelledby={head}>
      <div className="section__head">
        <h2 className="section__title" id={head}>
          <button type="button" className="section__fold" aria-expanded={!fold.folded} aria-controls={id ? `${id}-body` : undefined}
            onClick={fold.onToggle} title={fold.folded ? `Show ${title.toLowerCase()}` : `Minimise ${title.toLowerCase()}`}>
            <ChevronDown size={15} aria-hidden="true" className="section__chevron" />
            {title}
          </button>
        </h2>
        {fold.folded && fold.summary ? <span className="section__summary">{fold.summary}</span> : null}
        {action}
      </div>
      {!fold.folded && <div id={id ? `${id}-body` : undefined}>{children}</div>}
    </section>
  );
}

export function Loading({ what }: { what: string }) {
  return <p className="state-note" role="status"><Loader2 size={16} aria-hidden="true" className="spin-when-motion" /> Loading {what}…</p>;
}

export function Empty({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return <div className="empty"><p>{children}</p>{action}</div>;
}

export function Unavailable({ children }: { children: ReactNode }) {
  return <div className="unavailable" role="note"><WifiOff size={16} aria-hidden="true" /><p>{children}</p></div>;
}

export function ErrorNote({ message, retry }: { message: string; retry?: () => void }) {
  return (
    <div className="error-note" role="alert">
      <XCircle size={16} aria-hidden="true" />
      <p>{message}</p>
      {retry && <Button variant="quiet" onClick={retry}><RefreshCw size={14} aria-hidden="true" /> Try again</Button>}
    </div>
  );
}

/** A resource's states in one place: loading, error (with retry), or the content. */
export function Loaded<T>({ resource, what, children }: {
  resource: { data: T | null; error: string | null; loading: boolean; reload: () => void }; what: string;
  children: (data: T) => ReactNode;
}) {
  if (resource.loading) return <Loading what={what} />;
  if (resource.error && !resource.data) {
    return <ErrorNote message={`The ${what} could not be loaded: ${resource.error}`} retry={resource.reload} />;
  }
  if (!resource.data) return null;
  return (
    <>
      {resource.error && <ErrorNote message={`Showing the last loaded ${what}; refreshing failed: ${resource.error}`} retry={resource.reload} />}
      {children(resource.data)}
    </>
  );
}

const RTF = typeof Intl !== "undefined" ? new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }) : null;

export function relative(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "never";
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return iso;
  const seconds = Math.round((at - now) / 1000);
  const abs = Math.abs(seconds);
  if (!RTF) return new Date(at).toLocaleString();
  if (abs < 45) return seconds <= 0 ? "just now" : "in a moment";
  if (abs < 3600) return RTF.format(Math.round(seconds / 60), "minute");
  if (abs < 86_400) return RTF.format(Math.round(seconds / 3600), "hour");
  if (abs < 86_400 * 14) return RTF.format(Math.round(seconds / 86_400), "day");
  return new Date(at).toLocaleDateString();
}

export function Time({ iso, prefix }: { iso: string | null | undefined; prefix?: string }) {
  if (!iso) return <span className="meta">{prefix ? `${prefix} ` : ""}never</span>;
  const exact = Number.isNaN(Date.parse(iso)) ? iso : new Date(iso).toLocaleString();
  return <time className="meta" dateTime={iso} title={exact}>{prefix ? `${prefix} ` : ""}{relative(iso)}</time>;
}
