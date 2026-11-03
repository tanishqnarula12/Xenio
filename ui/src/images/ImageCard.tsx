import { Check, Copy as Layers, Download, ImageOff, Maximize2, Pencil, RefreshCw, RotateCcw, Square, X } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import type { ImageJob } from "../api/types";
import { useApp, useAppState } from "../state/context";
import { GenerationMark } from "./GenerationMark";

const ACTIVE = new Set(["preparing", "sending", "generating", "receiving"]);
const WORDS: Record<string, string> = { preparing: "Preparing the prompt", sending: "Sending the request", generating: "Generating", receiving: "Receiving the image", complete: "Complete", failed: "Failed", cancelled: "Cancelled" };
const POLL_MS = 1500; // a safety net: XENIO reports each change as it happens

function ratio(size: string): string {
  const [w, h] = size.split("x").map(Number);
  return w && h ? `${w} / ${h}` : "1 / 1";
}

/** Never taller than most of a window, whatever its shape: the whole picture stays in view. */
function maxWidth(size: string): string {
  const [w, h] = size.split("x").map(Number);
  return w && h ? `calc(var(--picture-h, 42vh) * ${(w / h).toFixed(4)})` : "var(--picture-h, 42vh)";
}

function useJob(id: string): ImageJob | null {
  const { app } = useApp();
  const pushed = useAppState((s) => s.images[id]);
  const [fetched, setFetched] = useState<ImageJob | null>(null);
  const job = pushed ?? fetched;
  const active = !job || ACTIVE.has(job.state);
  useEffect(() => {
    if (!active && job) return;
    let live = true;
    const read = () => app.api.image(id).then((j) => { if (live) setFetched(j); }, () => undefined);
    void read();
    const t = setInterval(() => void read(), POLL_MS);
    return () => { live = false; clearInterval(t); };
  }, [app, id, active]); // eslint-disable-line react-hooks/exhaustive-deps
  return pushed && fetched && (fetched.updated ?? 0) > (pushed.updated ?? 0) ? fetched : job;
}

function usePicture(id: string, ready: boolean): string | null {
  const { app } = useApp();
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!ready) return;
    const control = new AbortController();
    let made: string | null = null;
    app.api.imageFile(id, control.signal).then((buffer) => {
      made = URL.createObjectURL(new Blob([buffer]));
      setUrl(made);
    }, () => undefined);
    return () => { control.abort(); if (made) URL.revokeObjectURL(made); };
  }, [app, id, ready]);
  return url;
}

function Lightbox({ url, alt, onClose }: { url: string; alt: string; onClose: () => void }) {
  const close = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    close.current?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="lightbox" role="dialog" aria-modal="true" aria-label="The picture, larger" onClick={onClose}>
      <button ref={close} type="button" className="lightbox__close" aria-label="Close" onClick={onClose}><X size={20} aria-hidden="true" /></button>
      <img src={url} alt={alt} onClick={(e) => e.stopPropagation()} />
    </div>
  );
}

/** A picture in the conversation: its real state while it is made, then the picture and what can be done with it. */
export function ImageCard({ id }: { id: string }) {
  const { app } = useApp();
  const job = useJob(id);
  const url = usePicture(id, job?.state === "complete");
  const [big, setBig] = useState(false);
  const [refining, setRefining] = useState(false);
  const [change, setChange] = useState("");
  const [more, setMore] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  const act = async (action: string, extra: Record<string, unknown> = {}) => {
    if (busy) return; // a second click makes no second picture
    setBusy(true); setError(null);
    try {
      const out = await app.api.changeImage(id, { action, ...extra });
      if (out.jobs) setMore((m) => [...m, ...out.jobs!.map((j) => j.id)]);
      else if (action === "save") setSaved(out.saved ?? "");
      else if (action !== "cancel" && out.id && out.id !== id) setMore((m) => [...m, out.id]);
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  const refine = async (e: FormEvent) => {
    e.preventDefault();
    if (!change.trim()) return;
    await act("refine", { change: change.trim() });
    setChange(""); setRefining(false);
  };
  if (!job) return <div className="image-card image-card--loading"><p className="meta">Looking for the picture…</p></div>;
  const active = ACTIVE.has(job.state);
  return (
    <div className="image-card">
      <div className={`image-frame ${job.state === "complete" && url ? "image-frame--done" : ""}`} style={{ aspectRatio: ratio(job.size), maxWidth: maxWidth(job.size) }}>
        {active && <GenerationMark state={job.state} />}
        {job.state === "complete" && url && <button type="button" className="image-open" aria-label="See the picture larger" onClick={() => setBig(true)}><img src={url} alt={job.prompt} /></button>}
        {job.state === "complete" && !url && <p className="meta image-frame__note">Opening the picture…</p>}
        {(job.state === "failed" || job.state === "cancelled") && <div className="image-frame__note"><ImageOff size={22} aria-hidden="true" /><p>{job.state === "cancelled" ? "Cancelled." : job.error?.message ?? "The picture could not be made."}</p></div>}
      </div>
      <div className="image-card__foot">
        <p className="image-card__prompt" title={job.prompt}>{job.prompt}</p>
        <p className={`image-state image-state--${job.state}`} role="status" aria-live="polite">{active ? `${WORDS[job.state]}…` : WORDS[job.state]}{job.state === "complete" && job.ms ? ` · ${(job.ms / 1000).toFixed(0)} s` : ""}</p>
        <div className="image-actions">
          {active && <button type="button" className="btn btn--quiet" disabled={busy} onClick={() => void act("cancel")}><Square size={13} aria-hidden="true" /> Cancel</button>}
          {(job.state === "failed" || job.state === "cancelled") && job.error?.kind !== "config" && <button type="button" className="btn" disabled={busy} onClick={() => void act("retry")}><RotateCcw size={13} aria-hidden="true" /> Try again</button>}
          {job.state === "complete" && (
            <>
              <button type="button" className="btn btn--quiet" onClick={() => setBig(true)} disabled={!url}><Maximize2 size={13} aria-hidden="true" /> Larger</button>
              <button type="button" className="btn btn--quiet" disabled={busy} onClick={() => void act("regenerate")}><RefreshCw size={13} aria-hidden="true" /> Regenerate</button>
              <button type="button" className="btn btn--quiet" disabled={busy} onClick={() => void act("variation", { count: 1 })}><Layers size={13} aria-hidden="true" /> Variation</button>
              <button type="button" className="btn btn--quiet" aria-expanded={refining} onClick={() => setRefining((r) => !r)}><Pencil size={13} aria-hidden="true" /> Refine</button>
              <button type="button" className="btn btn--quiet" disabled={busy || saved !== null} onClick={() => void act("save")}>{saved !== null ? <><Check size={13} aria-hidden="true" /> Saved</> : <><Download size={13} aria-hidden="true" /> Save</>}</button>
            </>
          )}
        </div>
        {refining && (
          <form className="image-refine" onSubmit={refine}>
            <label className="sr-only" htmlFor={`refine-${id}`}>What should change?</label>
            <input id={`refine-${id}`} value={change} autoFocus maxLength={400} placeholder="What should change? The original stays." onChange={(e) => setChange(e.target.value)} onKeyDown={(e) => { if (e.key === "Escape") setRefining(false); }} />
            <button type="submit" className="btn btn--primary" disabled={busy || !change.trim()}>Make it</button>
          </form>
        )}
        {job.cropped_from && <p className="meta">{job.size.replace("x", " × ")}, cut from the {job.cropped_from.replace("x", " × ")} picture the model makes. The whole picture is kept.</p>}
        {saved && <p className="meta" role="status">Saved to {saved}</p>}
        {error && <p className="voice-error" role="alert">{error}</p>}
      </div>
      {big && url && <Lightbox url={url} alt={job.prompt} onClose={() => setBig(false)} />}
      {more.map((other) => <ImageCard key={other} id={other} />)}
    </div>
  );
}
