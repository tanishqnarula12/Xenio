import { Check, ChevronLeft, ChevronRight, Pause, Play, RotateCcw, Shuffle, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type PointerEvent } from "react";
import type { Activity, Flashcard } from "../api/types";
import { useReducedMotion } from "../motion/motion";
import { Inline } from "../components/RichText";
import { CodeRunner, runnable } from "./code";
import { compile, format } from "./expression";

const COLOURS = ["#a9c8ff", "#57c78f", "#e8b84f", "#f27474"];
const W = 640, H = 340, PAD = { l: 52, r: 16, t: 14, b: 40 };

function niceTicks(low: number, high: number, count = 6): number[] {
  const span = high - low;
  if (!(span > 0)) return [low];
  const raw = span / count, power = 10 ** Math.floor(Math.log10(raw)), step = [1, 2, 2.5, 5, 10].map((s) => s * power).find((s) => s >= raw) ?? raw;
  const out: number[] = [];
  for (let v = Math.ceil(low / step) * step; v <= high + step / 1000; v += step) out.push(Number(v.toFixed(10)));
  return out;
}

function Heading({ a }: { a: { title?: string; prompt?: string } }) {
  return <>{a.title && <h4 className="activity__title">{a.title}</h4>}{a.prompt && <p className="activity__prompt"><Inline text={a.prompt} /></p>}</>;
}

export function GraphExplorer({ a }: { a: Extract<Activity, { type: "graph" }> }) {
  const [values, setValues] = useState<Record<string, number>>(() => Object.fromEntries(a.params.map((p) => [p.name, p.value])));
  const [hover, setHover] = useState<number | null>(null);
  const curves = useMemo(() => a.functions.map((f) => { try { return { ...f, fn: compile(f.expr) }; } catch { return null; } }).filter((c): c is NonNullable<typeof c> => Boolean(c)), [a.functions]);
  const { min: xLow, max: xHigh } = a.x;
  const samples = useMemo(() => curves.map((c) => Array.from({ length: 241 }, (_, i) => {
    const x = xLow + ((xHigh - xLow) * i) / 240;
    let y = NaN;
    try { y = c.fn({ ...values, x }); } catch { /* drawn as a gap */ }
    return { x, y: Number.isFinite(y) ? y : NaN };
  })), [curves, values, xLow, xHigh]);
  const [yLow, yHigh] = useMemo(() => {
    if (a.y.min != null && a.y.max != null && a.y.min < a.y.max) return [a.y.min, a.y.max];
    const ys = samples.flat().map((p) => p.y).filter(Number.isFinite).sort((p, q) => p - q);
    if (!ys.length) return [-1, 1];
    let low = ys[Math.floor(ys.length * 0.02)]!, high = ys[Math.floor(ys.length * 0.98)]!; // one spike does not flatten the rest
    if (a.y.min != null) low = a.y.min;
    if (a.y.max != null) high = a.y.max;
    if (low === high) { low -= 1; high += 1; }
    const margin = (high - low) * 0.08;
    return [low - margin, high + margin];
  }, [samples, a.y]);
  const sx = (x: number) => PAD.l + ((x - xLow) / (xHigh - xLow)) * (W - PAD.l - PAD.r);
  const sy = (y: number) => H - PAD.b - ((y - yLow) / (yHigh - yLow)) * (H - PAD.t - PAD.b);
  const path = (points: { x: number; y: number }[]) => {
    let d = "", pen = false;
    for (const p of points) {
      if (!Number.isFinite(p.y) || p.y < yLow - (yHigh - yLow) * 3 || p.y > yHigh + (yHigh - yLow) * 3) { pen = false; continue; }
      d += `${pen ? "L" : "M"}${sx(p.x).toFixed(1)} ${sy(p.y).toFixed(1)} `;
      pen = true;
    }
    return d;
  };
  const onMove = (e: PointerEvent<SVGSVGElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const x = ((e.clientX - r.left) / r.width) * W;
    setHover(x < PAD.l || x > W - PAD.r ? null : xLow + ((x - PAD.l) / (W - PAD.l - PAD.r)) * (xHigh - xLow));
  };
  const at = (fn: (v: Record<string, number>) => number, x: number) => { try { return fn({ ...values, x }); } catch { return NaN; } };
  const summary = curves.map((c) => `${c.label}: at x = ${format(xLow, 2)} it is ${format(at(c.fn, xLow))}, at x = ${format(xHigh, 2)} it is ${format(at(c.fn, xHigh))}`).join(". ");
  return (
    <div className="activity activity--graph">
      <Heading a={a} />
      <svg viewBox={`0 0 ${W} ${H}`} className="graph" role="img" aria-label={summary} onPointerMove={onMove} onPointerLeave={() => setHover(null)}>
        {niceTicks(xLow, xHigh).map((t) => <g key={`x${t}`}><line x1={sx(t)} x2={sx(t)} y1={PAD.t} y2={H - PAD.b} className="graph__grid" /><text x={sx(t)} y={H - PAD.b + 16} textAnchor="middle" className="graph__tick">{format(t, 2)}</text></g>)}
        {niceTicks(yLow, yHigh).map((t) => <g key={`y${t}`}><line x1={PAD.l} x2={W - PAD.r} y1={sy(t)} y2={sy(t)} className="graph__grid" /><text x={PAD.l - 8} y={sy(t) + 4} textAnchor="end" className="graph__tick">{format(t, 2)}</text></g>)}
        {yLow < 0 && yHigh > 0 && <line x1={PAD.l} x2={W - PAD.r} y1={sy(0)} y2={sy(0)} className="graph__axis" />}
        {xLow < 0 && xHigh > 0 && <line x1={sx(0)} x2={sx(0)} y1={PAD.t} y2={H - PAD.b} className="graph__axis" />}
        {a.x.label && <text x={(PAD.l + W - PAD.r) / 2} y={H - 6} textAnchor="middle" className="graph__label">{a.x.label}</text>}
        {a.y.label && <text x={12} y={(PAD.t + H - PAD.b) / 2} textAnchor="middle" transform={`rotate(-90 12 ${(PAD.t + H - PAD.b) / 2})`} className="graph__label">{a.y.label}</text>}
        <clipPath id="graph-plot"><rect x={PAD.l} y={PAD.t} width={W - PAD.l - PAD.r} height={H - PAD.t - PAD.b} /></clipPath>
        <g clipPath="url(#graph-plot)">{samples.map((points, i) => <path key={i} d={path(points)} fill="none" stroke={COLOURS[i % 4]} strokeWidth={2.4} strokeLinejoin="round" />)}</g>
        {(a.points ?? []).map((p, i) => <g key={i}><circle cx={sx(p.x)} cy={sy(p.y)} r={4} className="graph__point" />{p.label && <text x={sx(p.x) + 8} y={sy(p.y) - 8} className="graph__tick graph__tick--note">{p.label}</text>}</g>)}
        {hover != null && <g><line x1={sx(hover)} x2={sx(hover)} y1={PAD.t} y2={H - PAD.b} className="graph__cursor" />{curves.map((c, i) => { const y = at(c.fn, hover); return Number.isFinite(y) && y >= yLow && y <= yHigh ? <circle key={i} cx={sx(hover)} cy={sy(y)} r={4} fill={COLOURS[i % 4]} /> : null; })}</g>}
      </svg>
      <p className="graph__read" aria-live="off">{hover != null ? <>x = <b>{format(hover, 2)}</b>{curves.map((c, i) => <span key={i} style={{ color: COLOURS[i % 4] }}>  {c.label} = <b>{format(at(c.fn, hover))}</b></span>)}</> : curves.length > 1 ? curves.map((c, i) => <span key={i} style={{ color: COLOURS[i % 4] }}>● {c.label}  </span>) : <span className="meta">Move across the graph to read values.</span>}</p>
      {a.params.length > 0 && (
        <div className="sliders">
          {a.params.map((p) => (
            <label key={p.name} className="slider">
              <span className="slider__name">{p.label}</span>
              <input type="range" min={p.min} max={p.max} step={p.step} value={values[p.name] ?? p.value} onChange={(e) => setValues((v) => ({ ...v, [p.name]: Number(e.target.value) }))} aria-label={p.label} />
              <output className="slider__value">{format(values[p.name] ?? p.value, 3)}</output>
            </label>
          ))}
          <button type="button" className="btn btn--quiet" onClick={() => setValues(Object.fromEntries(a.params.map((p) => [p.name, p.value])))}><RotateCcw size={14} aria-hidden="true" /> Reset</button>
        </div>
      )}
    </div>
  );
}

export function Calculator({ a }: { a: Extract<Activity, { type: "calculator" }> }) {
  const [raw, setRaw] = useState<Record<string, string>>(() => Object.fromEntries(a.inputs.map((i) => [i.name, String(i.value)])));
  const values = Object.fromEntries(a.inputs.map((i) => [i.name, Number(raw[i.name])]));
  const compiled = useMemo(() => a.outputs.map((o) => { try { return compile(o.expr); } catch { return null; } }), [a.outputs]);
  return (
    <div className="activity activity--calc">
      <Heading a={a} />
      <div className="calc">
        <div className="calc__inputs">
          {a.inputs.map((i) => (
            <label key={i.name}><span>{i.label}</span><span className="calc__field"><input type="number" step="any" value={raw[i.name] ?? ""} onChange={(e) => setRaw((r) => ({ ...r, [i.name]: e.target.value }))} />{i.unit && <em>{i.unit}</em>}</span></label>
          ))}
        </div>
        <div className="calc__outputs" role="status" aria-live="polite">
          {a.outputs.map((o, n) => {
            let value = NaN;
            try { value = compiled[n]?.(values) ?? NaN; } catch { /* shown as a dash */ }
            return <div key={o.label + n} className="calc__out"><span>{o.label}</span><strong>{/^[$€£₹¥]$/.test((o.unit ?? "").trim()) ? `${o.unit!.trim()}${format(value, o.digits)}` : `${format(value, o.digits)}${o.unit ? ` ${o.unit}` : ""}`}</strong></div>;
          })}
        </div>
      </div>
    </div>
  );
}

export function ChartView({ a }: { a: Extract<Activity, { type: "chart" }> }) {
  const all = a.series.flatMap((s) => s.values);
  const high = Math.max(0, ...all), low = Math.min(0, ...all);
  const sy = (v: number) => H - PAD.b - ((v - low) / ((high - low) || 1)) * (H - PAD.t - PAD.b);
  const band = (W - PAD.l - PAD.r) / a.labels.length;
  return (
    <div className="activity activity--chart">
      <Heading a={a} />
      <svg viewBox={`0 0 ${W} ${H}`} className="graph" role="img" aria-label={a.series.map((s) => `${s.name}: ${a.labels.map((l, i) => `${l} ${s.values[i]}`).join(", ")}`).join(". ")}>
        {niceTicks(low, high).map((t) => <g key={t}><line x1={PAD.l} x2={W - PAD.r} y1={sy(t)} y2={sy(t)} className="graph__grid" /><text x={PAD.l - 8} y={sy(t) + 4} textAnchor="end" className="graph__tick">{format(t, 2)}</text></g>)}
        {a.labels.map((l, i) => <text key={i} x={PAD.l + band * (i + 0.5)} y={H - PAD.b + 16} textAnchor="middle" className="graph__tick">{l}</text>)}
        {a.chart === "line" ? a.series.map((s, n) => <g key={n}><path d={s.values.map((v, i) => `${i ? "L" : "M"}${PAD.l + band * (i + 0.5)} ${sy(v)}`).join(" ")} fill="none" stroke={COLOURS[n % 4]} strokeWidth={2.4} />{s.values.map((v, i) => <circle key={i} cx={PAD.l + band * (i + 0.5)} cy={sy(v)} r={3.5} fill={COLOURS[n % 4]}><title>{`${a.labels[i]}: ${v}`}</title></circle>)}</g>)
          : a.series.map((s, n) => s.values.map((v, i) => { const w = (band * 0.72) / a.series.length; return <rect key={`${n}-${i}`} x={PAD.l + band * (i + 0.14) + w * n} y={Math.min(sy(v), sy(0))} width={w} height={Math.abs(sy(v) - sy(0))} fill={COLOURS[n % 4]} rx={2}><title>{`${a.labels[i]}: ${v}`}</title></rect>; }))}
        {a.x_label && <text x={(PAD.l + W - PAD.r) / 2} y={H - 6} textAnchor="middle" className="graph__label">{a.x_label}</text>}
        {a.y_label && <text x={12} y={(PAD.t + H - PAD.b) / 2} textAnchor="middle" transform={`rotate(-90 12 ${(PAD.t + H - PAD.b) / 2})`} className="graph__label">{a.y_label}</text>}
      </svg>
      {a.series.length > 1 && <p className="graph__read">{a.series.map((s, n) => <span key={n} style={{ color: COLOURS[n % 4] }}>● {s.name}  </span>)}</p>}
    </div>
  );
}

export function StepThrough({ a }: { a: Extract<Activity, { type: "steps" }> }) {
  const [at, setAt] = useState(0);
  const [shown, setShown] = useState<Set<number>>(new Set());
  const step = a.steps[at]!;
  const hidden = Boolean(step.predict) && !shown.has(at);
  return (
    <div className="activity activity--steps">
      <Heading a={a} />
      <ol className="stepper" aria-label="Progress">{a.steps.map((_, i) => <li key={i} className={i === at ? "is-on" : i < at ? "is-done" : ""}><button type="button" aria-label={`Step ${i + 1}`} aria-current={i === at ? "step" : undefined} onClick={() => setAt(i)} /></li>)}</ol>
      <div className="step" aria-live="polite">
        <p className="meta">Step {at + 1} of {a.steps.length}</p>
        {step.title && <h5>{step.title}</h5>}
        {hidden ? (
          <div className="predict"><p><strong>Think first:</strong> <Inline text={step.predict} /></p><button type="button" className="btn btn--primary" onClick={() => setShown((s) => new Set(s).add(at))}>Show me</button></div>
        ) : (
          <>{step.predict && <p className="meta"><Inline text={step.predict} /></p>}<p><Inline text={step.body} /></p>{step.code && <pre className="term__code term__code--plain"><code>{step.code}</code></pre>}</>
        )}
      </div>
      <div className="activity__row">
        <button type="button" className="btn btn--quiet" disabled={at === 0} onClick={() => setAt(at - 1)}><ChevronLeft size={14} aria-hidden="true" /> Back</button>
        <button type="button" className="btn" disabled={at === a.steps.length - 1 || hidden} onClick={() => setAt(at + 1)}>Next <ChevronRight size={14} aria-hidden="true" /></button>
      </div>
    </div>
  );
}

export function Frames({ a }: { a: Extract<Activity, { type: "frames" }> }) {
  const [at, setAt] = useState(0);
  const [playing, setPlaying] = useState(false);
  const reduced = useReducedMotion();
  useEffect(() => {
    if (!playing) return;
    if (at >= a.frames.length - 1) { setPlaying(false); return; }
    const t = setTimeout(() => setAt((n) => n + 1), 1400);
    return () => clearTimeout(t);
  }, [playing, at, a.frames.length]);
  const frame = a.frames[at]!;
  return (
    <div className="activity activity--frames">
      <Heading a={a} />
      <div className={`frames ${reduced ? "frames--still" : ""}`} role="group" aria-label={`Frame ${at + 1} of ${a.frames.length}`}>
        <div className="frames__cells">
          {frame.cells.map((cell, i) => {
            const marks = Object.entries(frame.pointers ?? {}).filter(([, p]) => p === i).map(([k]) => k);
            return <div key={i} className={`cell ${frame.highlight?.includes(i) ? "cell--on" : ""} ${frame.dim?.includes(i) ? "cell--dim" : ""}`}><span className="cell__index">{i}</span><span className="cell__value">{cell}</span><span className="cell__pointer">{marks.join(" ")}</span></div>;
          })}
        </div>
        <p className="frames__note" aria-live="polite"><Inline text={frame.note} /></p>
      </div>
      <div className="activity__row frames__controls">
        <button type="button" className="btn btn--quiet" disabled={at === 0} onClick={() => { setPlaying(false); setAt(at - 1); }} aria-label="Previous frame"><ChevronLeft size={14} aria-hidden="true" /></button>
        <button type="button" className="btn" onClick={() => { if (at >= a.frames.length - 1) setAt(0); setPlaying((p) => !p); }}>{playing ? <><Pause size={14} aria-hidden="true" /> Pause</> : <><Play size={14} aria-hidden="true" /> Play</>}</button>
        <button type="button" className="btn btn--quiet" disabled={at === a.frames.length - 1} onClick={() => { setPlaying(false); setAt(at + 1); }} aria-label="Next frame"><ChevronRight size={14} aria-hidden="true" /></button>
        <input type="range" min={0} max={a.frames.length - 1} value={at} aria-label="Frame" onChange={(e) => { setPlaying(false); setAt(Number(e.target.value)); }} />
        <span className="meta tabular">{at + 1} / {a.frames.length}</span>
      </div>
    </div>
  );
}

function shuffled<T>(items: T[]): T[] {
  const out = [...items];
  for (let tries = 0; tries < 6; tries += 1) {
    for (let i = out.length - 1; i > 0; i -= 1) { const j = Math.floor(Math.random() * (i + 1)); [out[i], out[j]] = [out[j]!, out[i]!]; }
    if (out.some((x, i) => x !== items[i])) break;
  }
  return out;
}

export function MatchGame({ a }: { a: Extract<Activity, { type: "match" }> }) {
  const [right] = useState(() => shuffled(a.pairs.map((p) => p[1]!)));
  const [left, setLeft] = useState<string | null>(null);
  const [done, setDone] = useState<Set<string>>(new Set());
  const [miss, setMiss] = useState<string | null>(null);
  const [wrong, setWrong] = useState(0);
  const pick = (meaning: string) => {
    if (!left) return;
    if (a.pairs.find((p) => p[0] === left)?.[1] === meaning) { setDone((d) => new Set(d).add(left)); setLeft(null); setMiss(null); } else { setMiss(meaning); setWrong((w) => w + 1); }
  };
  const finished = done.size === a.pairs.length;
  const matched = new Set(a.pairs.filter((p) => done.has(p[0]!)).map((p) => p[1]!));
  return (
    <div className="activity activity--match">
      <Heading a={a} />
      <div className="match">
        <div className="match__col">{a.pairs.map(([term]) => <button type="button" key={term} disabled={done.has(term!)} aria-pressed={left === term} className={`match__item ${left === term ? "is-on" : ""} ${done.has(term!) ? "is-done" : ""}`} onClick={() => { setLeft(term!); setMiss(null); }}>{done.has(term!) && <Check size={14} aria-label="Matched" />} {term}</button>)}</div>
        <div className="match__col">{right.map((meaning) => <button type="button" key={meaning} disabled={matched.has(meaning) || !left} className={`match__item ${matched.has(meaning) ? "is-done" : ""} ${miss === meaning ? "is-miss" : ""}`} onClick={() => pick(meaning)}>{meaning}</button>)}</div>
      </div>
      <p className="meta" role="status">{finished ? `All matched${wrong ? `, with ${wrong} wrong ${wrong === 1 ? "try" : "tries"}` : ", first time"}.` : left ? `Now pick what goes with "${left}".` : "Pick something on the left."}</p>
    </div>
  );
}

export function OrderGame({ a }: { a: Extract<Activity, { type: "order" }> }) {
  const [items, setItems] = useState(() => shuffled(a.items));
  const [checked, setChecked] = useState(false);
  const move = (i: number, by: number) => {
    const next = [...items];
    [next[i], next[i + by]] = [next[i + by]!, next[i]!];
    setItems(next);
    setChecked(false);
  };
  const right = items.every((x, i) => x === a.items[i]);
  return (
    <div className="activity activity--order">
      <Heading a={a} />
      <ol className="order">
        {items.map((item, i) => (
          <li key={item} className={checked ? (item === a.items[i] ? "is-right" : "is-wrong") : ""}>
            <span className="order__n">{i + 1}</span><span className="order__text">{item}</span>
            <span className="order__moves">
              <button type="button" aria-label={`Move "${item}" up`} disabled={i === 0} onClick={() => move(i, -1)}><ChevronLeft size={14} style={{ transform: "rotate(90deg)" }} aria-hidden="true" /></button>
              <button type="button" aria-label={`Move "${item}" down`} disabled={i === items.length - 1} onClick={() => move(i, 1)}><ChevronRight size={14} style={{ transform: "rotate(90deg)" }} aria-hidden="true" /></button>
            </span>
          </li>
        ))}
      </ol>
      <div className="activity__row">
        <button type="button" className="btn btn--primary" onClick={() => setChecked(true)}><Check size={14} aria-hidden="true" /> Check the order</button>
        <button type="button" className="btn btn--quiet" onClick={() => { setItems(shuffled(a.items)); setChecked(false); }}><Shuffle size={14} aria-hidden="true" /> Mix again</button>
        {checked && <span role="status" className="meta">{right ? "That is the right order." : `${items.filter((x, i) => x === a.items[i]).length} of ${items.length} are in the right place.`}</span>}
      </div>
    </div>
  );
}

export function Flashcards({ cards }: { cards: Flashcard[] }) {
  const [at, setAt] = useState(0);
  const [flipped, setFlipped] = useState(false);
  const [known, setKnown] = useState<Set<number>>(new Set());
  const button = useRef<HTMLButtonElement>(null);
  if (!cards.length) return null;
  const card = cards[at]!;
  const go = (n: number) => { setAt((n + cards.length) % cards.length); setFlipped(false); };
  return (
    <div className="activity activity--cards">
      <button ref={button} type="button" className={`flash ${flipped ? "is-flipped" : ""}`} onClick={() => setFlipped((f) => !f)} aria-label={flipped ? `Answer: ${card.a}. Press to see the question.` : `Question: ${card.q}. Press to see the answer.`}>
        <span className="flash__side">{flipped ? "Answer" : "Question"}</span>
        <span className="flash__text"><Inline text={flipped ? card.a : card.q} /></span>
        <span className="flash__hint meta">{flipped ? "" : "Tap to flip"}</span>
      </button>
      <div className="activity__row">
        <button type="button" className="btn btn--quiet" onClick={() => go(at - 1)} aria-label="Previous card"><ChevronLeft size={14} aria-hidden="true" /></button>
        <span className="meta tabular">{at + 1} / {cards.length} · {known.size} known</span>
        <button type="button" className="btn btn--quiet" onClick={() => go(at + 1)} aria-label="Next card"><ChevronRight size={14} aria-hidden="true" /></button>
        {flipped && <><button type="button" className="btn" onClick={() => { setKnown((k) => new Set(k).add(at)); go(at + 1); }}><Check size={14} aria-hidden="true" /> I knew it</button><button type="button" className="btn btn--quiet" onClick={() => { setKnown((k) => { const n = new Set(k); n.delete(at); return n; }); go(at + 1); }}><X size={14} aria-hidden="true" /> Again</button></>}
      </div>
    </div>
  );
}

export function ActivityView({ a, lesson, index }: { a: Activity; lesson?: number; index?: number }) {
  switch (a.type) {
    case "graph": return <GraphExplorer a={a} />;
    case "calculator": return <Calculator a={a} />;
    case "chart": return <ChartView a={a} />;
    case "steps": return <StepThrough a={a} />;
    case "frames": return <Frames a={a} />;
    case "match": return <MatchGame a={a} />;
    case "order": return <OrderGame a={a} />;
    case "code": {
      const language = runnable(a.language);
      return language ? <CodeRunner language={language} code={a.code} stdin={a.stdin} task={a.task} checks={a.checks} solution={a.solution} lesson={lesson} index={index} prompt={a.prompt} title={a.title} /> : null;
    }
    default: return null;
  }
}
