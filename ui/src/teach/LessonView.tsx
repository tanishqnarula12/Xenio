import { ArrowRight, BookOpen, CheckCircle2, ChevronRight, CircleHelp, Lightbulb, MessageCircleQuestion, ShieldCheck, Sparkles, TriangleAlert } from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import type { Activity, Lesson, LessonDiagram, LessonQuestion } from "../api/types";
import { RichText } from "../components/RichText";
import { ErrorNote } from "../components/ui";
import { post } from "../overlay/native";
import { useApp } from "../state/context";
import { href } from "../state/router";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { Inline } from "../components/RichText";
import { ActivityView, Flashcards } from "./activities";
import { MermaidView } from "./Mermaid";

const REVIEW_POLL_MS = 4000;

export function DiagramView({ diagram }: { diagram: LessonDiagram }) {
  return (
    <figure className="activity activity--diagram">
      {diagram.title && <h4 className="activity__title">{diagram.title}</h4>}
      {diagram.mermaid && <MermaidView source={diagram.mermaid} alt={diagram.alt} notes={diagram.notes} />}
      {diagram.events && <ol className="timeline">{diagram.events.map((e, i) => <li key={i}><span className="timeline__when">{e.when}</span><span>{e.what}</span></li>)}</ol>}
      {diagram.columns && (
        <div className="compare-wrap"><table className="compare"><thead><tr>{diagram.columns.map((c) => <th key={c} scope="col">{c}</th>)}</tr></thead>
          <tbody>{diagram.rows?.map((r, i) => <tr key={i}>{r.map((c, j) => (j === 0 ? <th key={j} scope="row">{c}</th> : <td key={j}>{c}</td>))}</tr>)}</tbody></table></div>
      )}
      {diagram.caption && <figcaption className="activity__prompt">{diagram.caption}</figcaption>}
    </figure>
  );
}

export function QuestionCard({ question, onChange }: { question: LessonQuestion; onChange?: () => void }) {
  const { app } = useApp();
  const [typed, setTyped] = useState("");
  const [hints, setHints] = useState<string[]>(question.hints ?? []);
  const [left, setLeft] = useState(question.hints_left);
  const [result, setResult] = useState<{ result?: string; feedback?: string; correct_answer?: string; explanation?: string; credit?: number; concept?: { state: string } } | null>(null);
  const [shown, setShown] = useState<{ answer?: string; explanation?: string } | null>(question.answer ? { answer: question.answer, explanation: question.explanation } : null);
  const [picked, setPicked] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const finished = Boolean(result) || question.state === "answered";

  const send = async (answer: string, choice: number | null = null) => {
    setBusy(true); setError(null); setPicked(choice);
    try {
      const out = await app.api.changeLearning({ action: "answer", id: question.id, answer });
      setResult(out as typeof result);
      onChange?.();
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  const hint = async () => {
    setBusy(true); setError(null);
    try {
      const out = await app.api.changeLearning({ action: "hint", id: question.id });
      if (out.hint) setHints((h) => [...h, String(out.hint)]);
      setLeft(Number(out.hints_left ?? 0));
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  const reveal = async () => {
    setBusy(true);
    try {
      const out = await app.api.changeLearning({ action: "reveal", id: question.id });
      setShown({ answer: String(out.answer ?? ""), explanation: String(out.explanation ?? "") });
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  const tone = result ? (result.credit === 1 ? "ok" : result.credit ? "part" : "no") : "";
  return (
    <div className={`question ${tone ? `question--${tone}` : ""}`}>
      <p className="question__tag"><CircleHelp size={14} aria-hidden="true" /> Check yourself</p>
      <p className="question__text"><Inline text={question.question} /></p>
      {question.options.length > 0 ? (
        <div className="options">
          {question.options.map((option, i) => {
            const right = result?.correct_answer === option, wrong = picked === i && result && !right;
            return <button type="button" key={i} disabled={busy || finished} className={`option ${right ? "is-right" : ""} ${wrong ? "is-wrong" : ""}`} onClick={() => void send(option, i)}><span className="option__letter">{String.fromCharCode(65 + i)}</span><span>{option}</span></button>;
          })}
        </div>
      ) : !finished && (
        <form className="question__open" onSubmit={(e) => { e.preventDefault(); if (typed.trim()) void send(typed.trim()); }}>
          <label className="sr-only" htmlFor={`q-${question.id}`}>Your answer</label>
          <textarea id={`q-${question.id}`} rows={3} value={typed} placeholder="In your own words…" onChange={(e) => setTyped(e.target.value)} />
          <button type="submit" className="btn btn--primary" disabled={busy || !typed.trim()}>{busy ? "Marking…" : "Check my answer"}</button>
        </form>
      )}
      {hints.map((h, i) => <p key={i} className="question__hint"><Lightbulb size={14} aria-hidden="true" /> {h}</p>)}
      {!finished && !shown && (
        <div className="activity__row">
          {left > 0 && <button type="button" className="btn btn--quiet" disabled={busy} onClick={() => void hint()}><Lightbulb size={14} aria-hidden="true" /> {hints.length ? "Another hint" : "A hint"}</button>}
          <button type="button" className="btn btn--quiet" disabled={busy} onClick={() => void reveal()}>Show the answer</button>
        </div>
      )}
      {result && (
        <div className="question__result" role="status">
          <p className="question__verdict">{result.credit === 1 ? <CheckCircle2 size={16} aria-hidden="true" /> : <TriangleAlert size={16} aria-hidden="true" />} {result.credit === 1 ? "Right." : result.credit ? "Partly right." : "Not quite."}</p>
          {result.feedback && <p><Inline text={result.feedback} /></p>}
          {result.credit !== 1 && result.correct_answer && <p><strong>Answer:</strong> {result.correct_answer}</p>}
          {result.explanation && result.explanation !== result.feedback && <p className="meta"><Inline text={result.explanation} /></p>}
        </div>
      )}
      {shown && !result && <div className="question__result" role="status"><p><strong>Answer:</strong> {shown.answer}</p>{shown.explanation && <p className="meta">{shown.explanation}</p>}<p className="meta">Seen before answering, so this one will come up again.</p></div>}
      {error && <ErrorNote message={error} />}
    </div>
  );
}

function AskMore({ heading }: { heading: string }) {
  const { app } = useApp();
  const ask = (words: string) => void app.send(`${words}: ${heading}`, "text", true);
  return (
    <div className="ask-more">
      <MessageCircleQuestion size={14} aria-hidden="true" />
      <button type="button" onClick={() => ask("Explain this another way")}>Explain another way</button>
      <button type="button" onClick={() => ask("Give me a different example of this")}>Another example</button>
      <button type="button" onClick={() => ask("Why does this work")}>Why?</button>
    </div>
  );
}

interface Stage { key: string; label: string; node: ReactNode }

function stagesOf(lesson: Lesson, refresh: () => void): Stage[] {
  const { pack, questions } = lesson;
  const stages: Stage[] = [];
  const add = (key: string, label: string, node: ReactNode) => stages.push({ key, label, node });
  const diagrams = [...pack.diagrams], activities: { a: Activity; index: number }[] = (pack.activities ?? []).map((a, index) => ({ a, index }));
  const asks = [...questions];
  (pack.sections ?? []).forEach((section, i) => {
    const d = diagrams.shift(), act = activities.shift();
    const q = i % 2 === 1 ? asks.shift() : undefined;
    add(`s${i}`, section.heading, (
      <section className="lesson-section">
        <h3>{section.heading}</h3>
        <RichText text={section.body} />
        <AskMore heading={section.heading} />
        {d && <ErrorBoundary what="This diagram"><DiagramView diagram={d} /></ErrorBoundary>}
        {act && <ErrorBoundary what="This activity"><ActivityView a={act.a} lesson={lesson.id} index={act.index} /></ErrorBoundary>}
        {q && <ErrorBoundary what="This question"><QuestionCard question={q} onChange={refresh} /></ErrorBoundary>}
      </section>
    ));
  });
  if (pack.worked_example) {
    const w = pack.worked_example;
    add("worked", "A worked example", (
      <section className="lesson-section">
        <h3>Work through one</h3>
        <p className="worked__problem"><Inline text={w.problem} /></p>
        <ol className="worked">{w.steps.map((step, i) => <li key={i}><span><Inline text={step} /></span></li>)}</ol>
        {w.result && <p className="worked__result"><Sparkles size={14} aria-hidden="true" /> <Inline text={w.result} /></p>}
      </section>
    ));
  }
  if (diagrams.length || activities.length) {
    add("more", "See it and try it", <section className="lesson-section"><h3>See it and try it</h3>{diagrams.map((d, i) => <ErrorBoundary key={i} what="This diagram"><DiagramView diagram={d} /></ErrorBoundary>)}{activities.map((x) => <ActivityView key={x.index} a={x.a} lesson={lesson.id} index={x.index} />)}</section>);
  }
  if (pack.common_mistakes?.length) {
    add("mistakes", "What people get wrong", (
      <section className="lesson-section">
        <h3>What people get wrong</h3>
        <div className="mistakes">{pack.common_mistakes.map((m, i) => <details key={i}><summary><Inline text={m.mistake} /></summary>{m.why && <p className="meta">Why it feels right: <Inline text={m.why} /></p>}<p><strong>What is true:</strong> <Inline text={m.fix} /></p></details>)}</div>
      </section>
    ));
  }
  if (pack.flashcards?.length) add("cards", "Flashcards", <section className="lesson-section"><h3>Flashcards</h3><Flashcards cards={pack.flashcards} /></section>);
  if (asks.length) add("quiz", "Show what you know", <section className="lesson-section"><h3>Show what you know</h3><p className="meta">Your answers here are what your progress is built from.</p>{asks.map((q) => <QuestionCard key={q.id} question={q} onChange={refresh} />)}</section>);
  return stages;
}

function ReviewLine({ review }: { review: Lesson["pack"]["review"] }) {
  if (!review) return null;
  if (review.state === "checking") return <p className="lesson-check meta" role="status"><ShieldCheck size={14} aria-hidden="true" /> A second reader is checking the facts and answers…</p>;
  if (review.state === "skipped") return <p className="lesson-check meta" role="status"><TriangleAlert size={14} aria-hidden="true" /> The second check could not run this time, so this has not been double-checked.</p>;
  const found = review.found ?? [];
  return found.length ? (
    <details className="lesson-check meta"><summary><ShieldCheck size={14} aria-hidden="true" /> Checked by a second reader: {found.length} {found.length === 1 ? "thing" : "things"} corrected</summary><ul>{found.map((f, i) => <li key={i}>{f}</li>)}</ul></details>
  ) : <p className="lesson-check meta"><ShieldCheck size={14} aria-hidden="true" /> Checked by a second reader: nothing to correct.</p>;
}

function useLesson(id: number) {
  const { app } = useApp();
  const [lesson, setLesson] = useState<Lesson | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let live = true;
    app.api.lesson(id).then((l) => { if (live) { setLesson(l); setError(null); } }, (e: Error) => { if (live) setError(e.message); });
    return () => { live = false; };
  }, [app, id, tick]);
  const checking = lesson?.pack.review?.state === "checking";
  useEffect(() => {
    if (!checking) return;
    const t = setTimeout(() => setTick((n) => n + 1), REVIEW_POLL_MS);
    return () => clearTimeout(t);
  }, [checking, tick]);
  return { lesson, error, refresh: () => setTick((n) => n + 1) };
}

/** The whole lesson, one stage at a time: read a little, see it, try it, answer. */
export function LessonPlayer({ id, onChange }: { id: number; onChange?: () => void }) {
  const { lesson, error, refresh } = useLesson(id);
  const memory = `xenio.lesson.${id}`;
  const [reached, setReached] = useState(() => { try { return Number(localStorage.getItem(memory) ?? 0); } catch { return 0; } });
  const [all, setAll] = useState(false);
  const stages = useMemo(() => (lesson ? stagesOf(lesson, () => { refresh(); onChange?.(); }) : []), [lesson]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { try { localStorage.setItem(memory, String(reached)); } catch { /* it just starts from the top next time */ } }, [memory, reached]);
  if (error) return <ErrorNote message={error} />;
  if (!lesson) return <p className="meta">Opening the lesson…</p>;
  const { pack } = lesson;
  const shown = all ? stages : stages.slice(0, reached + 1);
  const left = stages.length - shown.length;
  return (
    <article className="lesson">
      <header className="lesson-hero">
        <p className="lesson-hero__kicker"><BookOpen size={14} aria-hidden="true" /> {lesson.topic}</p>
        <h2>{pack.title}</h2>
        {pack.why_it_matters && <p className="lesson-hero__why">{pack.why_it_matters}</p>}
        {pack.overview && <p className="lesson-hero__overview">{pack.overview}</p>}
        {pack.analogy && <p className="callout"><Lightbulb size={15} aria-hidden="true" /> <span>{pack.analogy}</span></p>}
        {!!pack.prerequisites?.length && <p className="meta">Helps to know first: {pack.prerequisites.join(" · ")}</p>}
        <ReviewLine review={pack.review} />
        {!!pack.missing?.length && <p className="meta">Some of this lesson could not be written this time: {pack.missing.join(", ")}.</p>}
        {stages.length > 1 && (
          <div className="lesson-progress">
            <div className="lesson-progress__bar" role="progressbar" aria-valuemin={0} aria-valuemax={stages.length} aria-valuenow={shown.length} aria-label="How far through the lesson"><span style={{ width: `${(shown.length / stages.length) * 100}%` }} /></div>
            <button type="button" className="btn btn--quiet" onClick={() => setAll((a) => !a)}>{all ? "One step at a time" : "Show it all"}</button>
          </div>
        )}
      </header>
      {shown.map((stage) => <div key={stage.key} className="lesson-stage">{stage.node}</div>)}
      {left > 0 && <button type="button" className="btn btn--primary lesson-next" onClick={() => setReached(shown.length)}>Next: {stages[shown.length]?.label} <ArrowRight size={15} aria-hidden="true" /></button>}
      {left === 0 && (
        <footer className="lesson-end">
          {!!pack.next_steps?.length && <><h3>Where to go next</h3><ul>{pack.next_steps.map((s, i) => <li key={i}><Inline text={s} /></li>)}</ul></>}
          {!!pack.uncertain?.length && <details open><summary>What to double-check</summary><ul>{pack.uncertain.map((u, i) => <li key={i}><Inline text={u} /></li>)}</ul></details>}
          {!!pack.sources?.length && <details open><summary>From your material</summary><ul>{pack.sources.map((s) => <li key={s.tag}><strong>[{s.tag}]</strong> {s.title}{s.page ? `, page ${s.page}` : ""}</li>)}</ul></details>}
        </footer>
      )}
    </article>
  );
}

/** A lesson in the chat: what it is, something to see or try right there, and the way into the whole thing. */
export function LessonCard({ id }: { id: number }) {
  const { lesson, error } = useLesson(id);
  if (error) return <ErrorNote message={error} />;
  if (!lesson) return <div className="lesson-card lesson-card--loading"><p className="meta">Opening the lesson…</p></div>;
  const { pack } = lesson;
  const first = pack.diagrams[0], activity = pack.activities?.[0];
  if (pack.single) {
    return <div className="lesson-card lesson-card--single">{first && <DiagramView diagram={first} />}{activity && <ActivityView a={activity} lesson={lesson.id} index={0} />}</div>;
  }
  const open = (e: React.MouseEvent) => {
    if (!window.webkit?.messageHandlers?.xenio || !document.documentElement.dataset.overlay) return;
    e.preventDefault();
    post({ type: "openApp", route: `learn/${lesson.topic_id}-${lesson.id}` });
  };
  return (
    <div className="lesson-card">
      <p className="lesson-hero__kicker"><BookOpen size={14} aria-hidden="true" /> Lesson</p>
      <h4>{pack.title}</h4>
      {first && <DiagramView diagram={first} />}
      <ReviewLine review={pack.review} />
      <p className="meta">{(pack.sections ?? []).length} parts · {pack.diagrams.length} diagrams · {(pack.activities ?? []).length} things to try · {pack.flashcards.length} flashcards · {lesson.questions.length} questions</p>
      <a className="btn btn--primary" href={href("learn", `${lesson.topic_id}-${lesson.id}`)} onClick={open}>Start the lesson <ChevronRight size={15} aria-hidden="true" /></a>
    </div>
  );
}
