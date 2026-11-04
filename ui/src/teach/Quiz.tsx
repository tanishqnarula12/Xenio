import { Check, ChevronRight, Lightbulb, RotateCcw, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { Inline } from "../components/RichText";
import { useOptionalApp } from "../state/context";
import { CodeBlock } from "./code";

// A quiz the teacher wrote, as a card: pick, commit, and only then see the answer, with why each option is right or wrong. Marking happens right here,
// so it is instant and costs nothing. Everything in it is shown as text: a quiz is a model's words.

export interface QuizOption { text: string; why: string; right: boolean }
export interface QuizQuestion { level: string; question: string; code: string; options: QuizOption[]; multi: boolean; hint: string; explain: string }
export interface Quiz { id: string; title: string; questions: QuizQuestion[] }
interface Progress { index: number; picked: number[][]; checked: boolean[]; hinted: boolean[]; finished: boolean }

const LETTERS = "ABCDEFGH";
const MAX_QUESTIONS = 12;
const REFERS_TO_OTHERS = /\b(?:above|both|neither|none of|all of|option [a-h]|[a-h] and [a-h])\b/i;

function hashOf(text: string): number {
  let h = 5381;
  for (let i = 0; i < text.length; i += 1) h = (h * 33 + text.charCodeAt(i)) >>> 0;
  return h;
}

/** Models favour certain positions for the right answer. This mixes them, the same way every time, unless an option refers to the others. */
function mixed<T extends QuizOption>(options: T[], seed: string): T[] {
  if (options.some((o) => REFERS_TO_OTHERS.test(o.text))) return options;
  let h = hashOf(seed);
  const out = [...options];
  for (let i = out.length - 1; i > 0; i -= 1) {
    h = (h * 1664525 + 1013904223) >>> 0;
    const j = h % (i + 1);
    [out[i], out[j]] = [out[j]!, out[i]!];
  }
  return out;
}

function positions(answer: unknown, count: number): number[] {
  const one = (value: unknown): number | null => {
    if (typeof value === "number" && Number.isInteger(value)) return value >= 0 && value < count ? value : null;
    const text = String(value ?? "").trim();
    if (/^\d+$/.test(text)) return Number(text) < count ? Number(text) : null;
    const letter = /^([A-Ha-h])(?:[).:\s]|$)/.exec(text);
    if (letter) { const at = LETTERS.indexOf(letter[1]!.toUpperCase()); return at < count ? at : null; }
    return null;
  };
  return (Array.isArray(answer) ? answer : [answer]).map(one).filter((n): n is number => n !== null);
}

function words(value: unknown): string {
  return typeof value === "string" || typeof value === "number" ? String(value).trim() : "";
}

/** What the model wrote, if it really is a quiz. Null means it is not, and the text is shown as it is rather than as a broken card. */
export function parseQuiz(source: string): Quiz | null {
  let data: unknown;
  try {
    data = JSON.parse(source);
  } catch {
    return null;
  }
  if (typeof data !== "object" || data === null) return null;
  const root = (Array.isArray(data) ? { questions: data } : data) as { title?: unknown; questions?: unknown; question?: unknown };
  const list = Array.isArray(root.questions) ? root.questions : root.question ? [root] : [];
  const questions: QuizQuestion[] = [];
  for (const raw of list.slice(0, MAX_QUESTIONS) as Record<string, unknown>[]) {
    if (!raw || typeof raw !== "object") continue;
    const question = words(raw.question);
    const given = Array.isArray(raw.options) ? (raw.options as unknown[]) : [];
    const cleaned = given.map((o) => (typeof o === "object" && o !== null ? { text: words((o as Record<string, unknown>).text), why: words((o as Record<string, unknown>).why), marked: Boolean((o as Record<string, unknown>).correct ?? (o as Record<string, unknown>).right) } : { text: words(o), why: "", marked: false })).filter((o) => o.text);
    const rightOnes = new Set(positions(raw.answer, cleaned.length));
    cleaned.forEach((o, i) => { if (o.marked) rightOnes.add(i); });
    if (!question || cleaned.length < 2 || !rightOnes.size || rightOnes.size === cleaned.length) continue;
    const options = cleaned.map((o, i) => ({ text: o.text, why: o.why, right: rightOnes.has(i) }));
    questions.push({ level: words(raw.level), question, code: words(raw.code), options: mixed(options, question), multi: Boolean(raw.multi) || rightOnes.size > 1, hint: words(raw.hint), explain: words(raw.explain) });
  }
  if (!questions.length) return null;
  return { id: String(hashOf(source)), title: words(root.title), questions };
}

function fresh(count: number): Progress {
  return { index: 0, picked: Array.from({ length: count }, () => []), checked: Array(count).fill(false), hinted: Array(count).fill(false), finished: false };
}

function remembered(id: string, count: number): Progress {
  try {
    const raw = localStorage.getItem(`xenio.quiz.${id}`);
    const data = raw ? (JSON.parse(raw) as Progress) : null;
    if (data && data.picked?.length === count && data.checked?.length === count) return data;
  } catch { /* no storage here: it simply starts fresh */ }
  return fresh(count);
}

const same = (a: number[], b: number[]) => a.length === b.length && [...a].sort().every((v, i) => v === [...b].sort()[i]);
const rightOnes = (q: QuizQuestion) => q.options.flatMap((o, i) => (o.right ? [i] : []));
const gotRight = (q: QuizQuestion, picked: number[]) => same(picked, rightOnes(q));

function Results({ quiz, progress, onRetry, onRetryMissed }: { quiz: Quiz; progress: Progress; onRetry: () => void; onRetryMissed: () => void }) {
  const ctx = useOptionalApp();
  const missed = quiz.questions.map((q, i) => ({ q, i })).filter(({ q, i }) => !gotRight(q, progress.picked[i] ?? []));
  const score = quiz.questions.length - missed.length;
  const share = score / quiz.questions.length;
  const line = share === 1 ? "All of them. You have this." : share >= 0.7 ? "Solid. A few details are worth another look." : share >= 0.4 ? "You have part of it. The misses below show where to look." : "This one is still new, and that is fine. Start with the misses below.";
  const radius = 26, around = 2 * Math.PI * radius;
  const ask = (text: string) => { void ctx?.app.send(text, "text", true); };
  const topic = quiz.title ? ` on ${quiz.title}` : "";
  return (
    <div className="quiz__results">
      <div className="quiz__score">
        <svg width="68" height="68" viewBox="0 0 68 68" aria-hidden="true">
          <circle cx="34" cy="34" r={radius} className="quiz__ring-back" />
          <circle cx="34" cy="34" r={radius} className="quiz__ring" strokeDasharray={around} strokeDashoffset={around * (1 - share)} transform="rotate(-90 34 34)" />
          <text x="34" y="39" textAnchor="middle" className="quiz__ring-text">{score}/{quiz.questions.length}</text>
        </svg>
        <div><p className="quiz__score-line">{score} of {quiz.questions.length} right</p><p className="quiz__muted">{line}</p></div>
      </div>
      {missed.length > 0 && (
        <ul className="quiz__missed" aria-label="What you missed">
          {missed.map(({ q, i }) => (
            <li key={i}><X size={14} aria-hidden="true" className="quiz__x" /><div><p><Inline text={q.question} /></p><p className="quiz__muted">The answer: <Inline text={q.options.filter((o) => o.right).map((o) => o.text).join("; ")} /></p></div></li>
          ))}
        </ul>
      )}
      <div className="quiz__actions">
        {missed.length > 0 && <button type="button" className="btn btn--primary" onClick={onRetryMissed}><RotateCcw size={14} aria-hidden="true" /> Retry the {missed.length === 1 ? "one" : missed.length} I missed</button>}
        <button type="button" className="btn btn--secondary" onClick={onRetry}>Start over</button>
        {ctx && missed.length > 0 && <button type="button" className="btn btn--quiet" onClick={() => ask(`Explain what I got wrong${topic}: ${missed.map(({ q }) => q.question).join(" | ").slice(0, 700)}`)}>Explain what I got wrong</button>}
        {ctx && <button type="button" className="btn btn--quiet" onClick={() => ask(`Give me harder questions${topic}`)}>Harder questions</button>}
      </div>
    </div>
  );
}

export function QuizCard({ quiz }: { quiz: Quiz }) {
  const count = quiz.questions.length;
  const [progress, setProgress] = useState<Progress>(() => remembered(quiz.id, count));
  const card = useRef<HTMLDivElement>(null);
  const next = useRef<HTMLButtonElement>(null);
  useEffect(() => { try { localStorage.setItem(`xenio.quiz.${quiz.id}`, JSON.stringify(progress)); } catch { /* only a convenience */ } }, [quiz.id, progress]);
  const index = Math.min(progress.index, count - 1);
  const q = quiz.questions[index]!;
  const picked = progress.picked[index] ?? [];
  const checked = progress.checked[index] ?? false;
  const right = checked && gotRight(q, picked);
  useEffect(() => { if (checked) next.current?.focus({ preventScroll: true }); }, [checked]);

  const change = (patch: (p: Progress) => Progress) => setProgress((p) => patch({ ...p, picked: [...p.picked], checked: [...p.checked], hinted: [...p.hinted] }));
  const pick = (at: number) => { if (!checked) change((p) => { const now = p.picked[index] ?? []; p.picked[index] = q.multi ? (now.includes(at) ? now.filter((n) => n !== at) : [...now, at]) : [at]; return p; }); };
  const check = () => { if (picked.length && !checked) change((p) => { p.checked[index] = true; return p; }); };
  const go = () => change((p) => (index + 1 >= count ? { ...p, finished: true } : { ...p, index: index + 1 }));
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (progress.finished || e.metaKey || e.ctrlKey || e.altKey) return;
    const letter = LETTERS.indexOf(e.key.toUpperCase());
    const digit = /^[1-8]$/.test(e.key) ? Number(e.key) - 1 : -1;
    const at = letter >= 0 ? letter : digit;
    if (at >= 0 && at < q.options.length) { e.preventDefault(); pick(at); return; }
    if (e.key === "Enter" && !checked && picked.length) { e.preventDefault(); check(); }
  };

  if (progress.finished) {
    return (
      <div className="quiz" ref={card}>
        {quiz.title && <p className="quiz__title">{quiz.title}</p>}
        <Results quiz={quiz} progress={progress} onRetry={() => setProgress(fresh(count))}
          onRetryMissed={() => setProgress((p) => { const again = fresh(count); quiz.questions.forEach((one, i) => { if (gotRight(one, p.picked[i] ?? [])) { again.picked[i] = p.picked[i]!; again.checked[i] = true; } }); again.index = again.checked.findIndex((c) => !c); return again; })} />
      </div>
    );
  }
  return (
    <div className="quiz" ref={card} onKeyDown={onKey} role="group" aria-label={quiz.title ? `Quiz: ${quiz.title}` : "Quiz"}>
      <div className="quiz__head">
        <p className="quiz__count">{quiz.title ? <><span className="quiz__title">{quiz.title}</span> · </> : null}Question {index + 1} of {count}</p>
        {q.level && <span className={`quiz__level quiz__level--${q.level.toLowerCase()}`}>{q.level}</span>}
      </div>
      {count > 1 && (
        <div className="quiz__steps" aria-hidden="true">
          {quiz.questions.map((one, i) => <span key={i} className={`quiz__step ${progress.checked[i] ? (gotRight(one, progress.picked[i] ?? []) ? "is-right" : "is-wrong") : i === index ? "is-now" : ""}`} />)}
        </div>
      )}
      <p className="quiz__question"><Inline text={q.question} /></p>
      {q.code && <CodeBlock code={q.code} language="" />}
      {q.multi && <p className="quiz__muted">Choose all that apply.</p>}
      <div className="quiz__options" role={q.multi ? "group" : "radiogroup"} aria-label="The options">
        {q.options.map((option, i) => {
          const on = picked.includes(i);
          const state = !checked ? (on ? "is-on" : "") : option.right ? "is-right" : on ? "is-wrong" : "is-out";
          return (
            <button type="button" key={i} role={q.multi ? "checkbox" : "radio"} aria-checked={on} aria-disabled={checked} className={`quiz__option ${state}`} onClick={() => pick(i)}>
              <span className="quiz__letter" aria-hidden="true">{checked && option.right ? <Check size={14} /> : checked && on ? <X size={14} /> : LETTERS[i]}</span>
              <span className="quiz__body">
                <span className="quiz__text"><Inline text={option.text} /></span>
                {checked && option.why && <span className="quiz__why"><Inline text={option.why} /></span>}
              </span>
            </button>
          );
        })}
      </div>
      {progress.hinted[index] && q.hint && !checked && <p className="quiz__hint"><Lightbulb size={14} aria-hidden="true" /> <span><Inline text={q.hint} /></span></p>}
      {checked && (
        <div className={`quiz__verdict ${right ? "is-right" : "is-wrong"}`} role="status">
          <p className="quiz__verdict-line">{right ? "That's right." : "Not quite."}</p>
          {q.explain && <p><Inline text={q.explain} /></p>}
        </div>
      )}
      <div className="quiz__actions">
        {!checked ? (
          <>
            <button type="button" className="btn btn--primary" disabled={!picked.length} onClick={check}>Check answer</button>
            {q.hint && !progress.hinted[index] && <button type="button" className="btn btn--quiet" onClick={() => change((p) => { p.hinted[index] = true; return p; })}><Lightbulb size={14} aria-hidden="true" /> Hint</button>}
          </>
        ) : (
          <button type="button" className="btn btn--primary" ref={next} onClick={go}>{index + 1 >= count ? "See how I did" : "Next question"} <ChevronRight size={14} aria-hidden="true" /></button>
        )}
      </div>
    </div>
  );
}

export function QuizBlock({ source, finished }: { source: string; finished: boolean }) {
  const quiz = useMemo(() => (finished ? parseQuiz(source) : null), [source, finished]);
  if (!finished) return <div className="quiz quiz--writing" role="status"><span className="quiz__pulse" aria-hidden="true" /> Writing your questions…</div>;
  if (!quiz) return <div className="quiz quiz--broken"><p className="quiz__muted">These questions came back in a form the card cannot read, so here they are as written. Ask again for a fresh set.</p><CodeBlock code={source} language="" /></div>;
  return <QuizCard quiz={quiz} />;
}
