import { BookOpen, CalendarClock, Check, FileUp, GraduationCap, Plus, Trash2, X } from "lucide-react";
import { useEffect, useRef, useState, type ChangeEvent, type FormEvent } from "react";
import type { LearningOverview, LearningTopic } from "../api/types";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { Button, Empty, ErrorNote, IconButton, Loaded, PageHeader, Section, Time } from "../components/ui";
import { useApp } from "../state/context";
import { href } from "../state/router";
import { useResource } from "../state/useResource";
import { LessonPlayer } from "../teach/LessonView";

const STATE_WORDS: Record<string, string> = { not_tried: "Not tried yet", needs_practice: "Needs practice", practising: "Getting there", demonstrated: "Shown you understand" };

function NewLesson({ data, onMade }: { data: LearningOverview; onMade: (topic: number, lesson: number) => void }) {
  const { app } = useApp();
  const [topic, setTopic] = useState("");
  const [level, setLevel] = useState("");
  const [goal, setGoal] = useState("");
  const [mode, setMode] = useState(data.default_mode);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      await app.api.changeLearning({ action: "start", topic: topic.trim(), level: level.trim(), goal: goal.trim(), mode });
      const made = await app.api.changeLearning({ action: "compose", topic: topic.trim(), mode });
      const overview = await app.api.learning();
      const found = overview.topics.find((t) => t.name.toLowerCase() === topic.trim().toLowerCase());
      if (found && made.lesson) onMade(found.id, Number(made.lesson));
    } catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  };
  return (
    <form className="learn-new" onSubmit={submit} aria-label="Learn something">
      <label className="learn-new__topic"><span className="sr-only">What do you want to learn?</span><input value={topic} maxLength={120} placeholder="What do you want to learn? Any subject." onChange={(e) => setTopic(e.target.value)} /></label>
      <div className="learn-new__row">
        <label><span>Your level</span><input value={level} maxLength={80} placeholder="class 10, beginner…" onChange={(e) => setLevel(e.target.value)} /></label>
        <label><span>Your goal</span><input value={goal} maxLength={200} placeholder="exam, curiosity, a project…" onChange={(e) => setGoal(e.target.value)} /></label>
        <label><span>How</span><select value={mode} onChange={(e) => setMode(e.target.value)}>{Object.entries(data.modes).map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label>
      </div>
      <Button variant="primary" type="submit" disabled={busy || !topic.trim() || !data.writer}>{busy ? "Writing your lesson…" : <><GraduationCap size={15} aria-hidden="true" /> Start learning</>}</Button>
      {busy && <p className="meta" role="status">Writing the chapter, diagrams, activities and practice. This usually takes under a minute, and you can use the rest of XENIO meanwhile.</p>}
      {!data.writer && <p className="meta">Writing full lessons needs a Nabh key (NABH_API_KEY). XENIO can still teach you in conversation.</p>}
      {error && <ErrorNote message={error} />}
    </form>
  );
}

function Materials({ topic, onChange }: { topic: LearningTopic; onChange: () => void }) {
  const { app } = useApp();
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notes, setNotes] = useState("");
  const add = async (e: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? []);
    e.target.value = "";
    if (!files.length) return;
    setBusy(true); setError(null); setNote(null);
    try {
      for (const file of files) {
        const saved = await app.api.upload(file);
        const made = await app.api.changeLearning({ action: "add_material", upload: saved.id, topic: topic.name });
        setNote(`Read ${made.title}: ${made.passages} passages, searchable ${made.search}.${Number(made.unread_pages) ? ` ${made.unread_pages} pages could not be read.` : ""}`);
      }
      onChange();
    } catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  };
  const paste = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      await app.api.changeLearning({ action: "add_material", text: notes, title: "My notes", topic: topic.name });
      setNotes(""); onChange();
    } catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  };
  return (
    <Section title="Your material" id="learn-material">
      <p className="meta">Notes, PDFs, textbooks, syllabi, code, photos of a page. Lessons on this subject use them first and cite the page. They are read by Nabh to make them searchable.</p>
      {topic.materials.length > 0 && (
        <ul className="learn-list">{topic.materials.map((m) => (
          <li key={m.id}><span><strong>{m.title}</strong><span className="meta"> · {m.kind}{m.pages ? ` · ${m.pages} pages` : ""}{m.ocr_pages ? ` · ${m.ocr_pages} read from scans` : ""}{m.unread_pages ? ` · ${m.unread_pages} unreadable` : ""}</span></span>
            <IconButton label={`Remove ${m.title}`} icon={Trash2} onClick={() => void app.api.changeLearning({ action: "delete_material", id: m.id }).then(onChange)} /></li>
        ))}</ul>
      )}
      <div className="activity__row">
        <input ref={input} type="file" multiple hidden aria-label="Add study material" accept=".pdf,.doc,.docx,.rtf,.txt,.md,.csv,.py,.js,.ts,.java,.c,.cpp,.h,.html,.ipynb,.png,.jpg,.jpeg,.heic,.webp" onChange={(e) => void add(e)} />
        <Button disabled={busy} onClick={() => input.current?.click()}><FileUp size={14} aria-hidden="true" /> {busy ? "Reading…" : "Add files"}</Button>
      </div>
      <form className="memory-edit" onSubmit={paste} aria-label="Paste notes">
        <label className="sr-only" htmlFor="learn-notes">Paste notes</label>
        <textarea id="learn-notes" rows={2} value={notes} placeholder="Or paste notes here…" onChange={(e) => setNotes(e.target.value)} />
        {notes.trim() && <Button type="submit" disabled={busy}><Plus size={14} aria-hidden="true" /> Add these notes</Button>}
      </form>
      {note && <p className="meta" role="status">{note}</p>}
      {error && <ErrorNote message={error} />}
    </Section>
  );
}

function TopicView({ id, lessonId, onChange }: { id: number; lessonId: number | null; onChange: () => void }) {
  const { app } = useApp();
  const topic = useResource((s) => app.api.learningTopic(id, s), ["learning"], [id]);
  const [lesson, setLesson] = useState<number | null>(lessonId);
  const [mode, setMode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [forgetting, setForgetting] = useState(false);
  useEffect(() => setLesson(lessonId), [lessonId, id]);
  return (
    <Loaded resource={topic} what="this subject">
      {(data) => {
        const current = lesson ?? data.lessons[0]?.id ?? null;
        const write = async (concept = "") => {
          setBusy(true); setError(null);
          try {
            const made = await app.api.changeLearning({ action: "compose", topic: data.name, concept, mode: mode || data.mode, fresh: Boolean(mode) || Boolean(concept) });
            setLesson(Number(made.lesson));
            onChange();
          } catch (err) { setError((err as Error).message); } finally { setBusy(false); }
        };
        return (
          <>
            <PageHeader title={data.name} intro={[data.level && `Level: ${data.level}`, data.goal && `Goal: ${data.goal}`].filter(Boolean).join(" · ") || "Your progress comes only from answers you really gave."} />
            {current != null ? <LessonPlayer key={current} id={current} onChange={onChange} /> : <Empty action={<Button variant="primary" disabled={busy} onClick={() => void write()}>{busy ? "Writing…" : "Write the first lesson"}</Button>}>No lesson yet.</Empty>}
            {error && <ErrorNote message={error} />}
            <Section title="Progress" id="learn-progress">
              {data.concepts.length ? (
                <ul className="concepts">{data.concepts.map((c) => (
                  <li key={c.id}>
                    <div className="concept__head"><strong>{c.name}</strong><span className={`concept__state concept__state--${c.state}`}>{STATE_WORDS[c.state] ?? c.state}</span></div>
                    <div className="concept__bar" role="img" aria-label={c.score == null ? "No answers yet" : `Score ${Math.round(c.score * 100)} percent from ${c.attempts} answers`}><span style={{ width: `${(c.score ?? 0) * 100}%` }} /></div>
                    <p className="meta">{c.attempts ? `${c.attempts} ${c.attempts === 1 ? "answer" : "answers"}, ${c.right_alone} right without help` : "No answers yet"}{c.is_due ? " · due for review now" : c.due ? ` · review ${new Date(c.due).toLocaleDateString(undefined, { day: "numeric", month: "short" })}` : ""}</p>
                    <button type="button" className="link-button" disabled={busy} onClick={() => void write(c.name)}>{busy ? "Writing…" : "A lesson on this"}</button>
                  </li>
                ))}</ul>
              ) : <Empty>The concepts appear with the first lesson.</Empty>}
            </Section>
            {data.mistakes.length > 0 && (
              <Section title="Mistakes to revisit" id="learn-mistakes">
                <ul className="learn-list">{data.mistakes.map((m, i) => <li key={i}><span>{m.misconception}{m.times > 1 && <span className="meta"> · {m.times} times</span>}</span></li>)}</ul>
                <Button disabled={busy} onClick={() => { setMode("mistakes"); void write(); }}>Review my mistakes</Button>
              </Section>
            )}
            <Materials topic={data} onChange={onChange} />
            {data.lessons.length > 0 && (
              <Section title="Lessons" id="learn-lessons">
                <ul className="learn-list">{data.lessons.map((l) => <li key={l.id}><button type="button" className="link-button" onClick={() => setLesson(l.id)}>{l.id === current ? <strong>{l.title}</strong> : l.title}</button><Time iso={l.created} /></li>)}</ul>
                <div className="activity__row">
                  <select aria-label="How" value={mode} onChange={(e) => setMode(e.target.value)}><option value="">The same way</option><option value="simple">Explain simply</option><option value="visual">Show visually</option><option value="hints">Give me hints</option><option value="quiz">Quiz me</option><option value="test">Test my understanding</option><option value="material">Study with my material</option></select>
                  <Button disabled={busy} onClick={() => void write()}>{busy ? "Writing…" : "Write a new lesson"}</Button>
                </div>
              </Section>
            )}
            {data.attempts.length > 0 && (
              <Section title="Your answers" id="learn-answers">
                <p className="meta">Everything your progress is built from. If one was marked wrongly, correct it: progress is worked out again.</p>
                <ul className="attempts">{data.attempts.map((a) => (
                  <li key={a.id}>
                    <p>{a.question}</p>
                    <p className="meta">You said “{a.answer}” · {a.credit === 1 ? "right" : a.credit ? "partly right" : "not right"}{a.hints_used ? " · with hints" : ""}{a.revealed ? " · after seeing the answer" : ""}{a.corrected ? " · corrected by you" : ""} · marked by {a.graded_by}</p>
                    <span className="attempts__actions">
                      {a.credit !== 1 && <IconButton label="Mark as right" icon={Check} onClick={() => void app.api.changeLearning({ action: "correct_attempt", id: a.id, credit: 1 }).then(onChange)} />}
                      {a.credit !== 0 && <IconButton label="Mark as wrong" icon={X} onClick={() => void app.api.changeLearning({ action: "correct_attempt", id: a.id, credit: 0 }).then(onChange)} />}
                      <IconButton label="Remove this answer" icon={Trash2} onClick={() => void app.api.changeLearning({ action: "delete_attempt", id: a.id }).then(onChange)} />
                    </span>
                  </li>
                ))}</ul>
              </Section>
            )}
            <Button variant="quiet" onClick={() => setForgetting(true)}><Trash2 size={14} aria-hidden="true" /> Forget this subject</Button>
            {forgetting && <ConfirmDialog title={`Forget ${data.name}?`} confirmLabel="Forget it" onCancel={() => setForgetting(false)} body="Its lessons, answers, progress and material are erased." onConfirm={() => { setForgetting(false); void app.api.changeLearning({ action: "forget_topic", id: data.id }).then(() => { window.location.hash = href("learn"); onChange(); }); }} />}
          </>
        );
      }}
    </Loaded>
  );
}

export function LearnPage({ param }: { param: string | null }) {
  const { app } = useApp();
  const overview = useResource((s) => app.api.learning(s), ["learning"], []);
  const [topicPart, lessonPart] = (param ?? "").split("-");
  const topicId = Number(topicPart) || null;
  const lessonId = Number(lessonPart) || null;
  const [tick, setTick] = useState(0);
  const refresh = () => { setTick((n) => n + 1); overview.reload?.(); };
  if (topicId) return <><p className="learn-back"><a className="link" href={href("learn")}>All subjects</a></p><TopicView key={`${topicId}-${tick >= 0 ? "" : tick}`} id={topicId} lessonId={lessonId} onChange={refresh} /></>;
  return (
    <>
      <PageHeader title="Learn" intro="Any subject, your way: read a little, see it, try it yourself, answer. Your progress comes only from what you really answer." />
      <Loaded resource={overview} what="your learning">
        {(data) => (
          <>
            <NewLesson data={data} onMade={(topic, lesson) => { window.location.hash = href("learn", `${topic}-${lesson}`); }} />
            {data.due.length > 0 && (
              <Section title="Due for review" id="learn-due">
                <ul className="learn-list">{data.due.map((d, i) => <li key={i}><CalendarClock size={14} aria-hidden="true" /> <span><strong>{d.concept}</strong><span className="meta"> · {d.topic}</span></span></li>)}</ul>
              </Section>
            )}
            <Section title="Your subjects" id="learn-subjects">
              {data.topics.length ? (
                <ul className="subject-grid">{data.topics.map((t) => (
                  <li key={t.id}><a href={href("learn", String(t.id))} className="subject">
                    <BookOpen size={16} aria-hidden="true" /><strong>{t.name}</strong>
                    <span className="meta">{t.concepts ? `${t.demonstrated} of ${t.concepts} shown` : "Just started"}{t.due ? ` · ${t.due} due` : ""}</span>
                    <span className="subject__bar"><span style={{ width: `${t.concepts ? (t.demonstrated / t.concepts) * 100 : 0}%` }} /></span>
                  </a></li>
                ))}</ul>
              ) : <Empty>Nothing yet. Name any subject above, or say “teach me photosynthesis”.</Empty>}
            </Section>
          </>
        )}
      </Loaded>
    </>
  );
}
