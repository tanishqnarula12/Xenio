import { Check, Copy, Eye, Play, RotateCcw, X } from "lucide-react";
import { useMemo, useState, type KeyboardEvent, type ReactNode } from "react";
import type { CodeRun, ExerciseResult } from "../api/types";
import { useApp } from "../state/context";

const KEYWORDS: Record<string, string> = {
  python: "and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass print raise return True try while with yield range len input int str float list dict set",
  javascript: "async await break case catch class const continue default delete do else export extends false finally for from function if import in instanceof let new null of return require super switch this throw true try typeof undefined var while yield console",
  c: "auto break case char const continue default do double else enum extern float for goto if include int long main printf register return scanf short signed sizeof static struct switch typedef union unsigned void volatile while define",
  cpp: "auto bool break case catch char class const continue cout cin endl default delete do double else enum false float for friend if include int long main namespace new nullptr operator private protected public return short signed sizeof static std string struct switch template this throw true try typedef typename using vector virtual void while",
};
const COMMENT: Record<string, RegExp> = { python: /#[^\n]*/y, javascript: /\/\/[^\n]*|\/\*[\s\S]*?\*\//y, c: /\/\/[^\n]*|\/\*[\s\S]*?\*\//y, cpp: /\/\/[^\n]*|\/\*[\s\S]*?\*\//y };
export const LANGUAGE_NAMES: Record<string, string> = { python: "Python", javascript: "JavaScript", c: "C", cpp: "C++" };
const RUN_LINE: Record<string, string> = { python: "python3 main.py", javascript: "node main.js", c: "clang main.c && ./main", cpp: "clang++ main.cpp && ./main" };

export function runnable(language: string): string | null {
  const low = language.trim().toLowerCase();
  const name = ({ py: "python", python3: "python", js: "javascript", node: "javascript", "c++": "cpp", cxx: "cpp" } as Record<string, string>)[low] ?? low;
  return name in LANGUAGE_NAMES ? name : null;
}

/** Colours for code, as React elements: the code is only ever text. */
export function highlight(code: string, language: string): ReactNode[] {
  const words = new Set((KEYWORDS[language] ?? "").split(" "));
  const comment = COMMENT[language];
  const out: ReactNode[] = [];
  let at = 0, plain = "", key = 0;
  const flush = () => { if (plain) { out.push(plain); plain = ""; } };
  while (at < code.length) {
    if (comment) {
      comment.lastIndex = at;
      const m = comment.exec(code);
      if (m) { flush(); out.push(<span key={key++} className="tok-comment">{m[0]}</span>); at += m[0].length; continue; }
    }
    const ch = code[at]!;
    if (ch === '"' || ch === "'" || ch === "`") {
      let end = at + 1;
      while (end < code.length && code[end] !== ch && code[end] !== "\n") end += code[end] === "\\" ? 2 : 1;
      flush();
      out.push(<span key={key++} className="tok-string">{code.slice(at, end + 1)}</span>);
      at = end + 1;
      continue;
    }
    if (/[A-Za-z_]/.test(ch)) {
      let end = at + 1;
      while (end < code.length && /\w/.test(code[end]!)) end += 1;
      const word = code.slice(at, end);
      if (words.has(word)) { flush(); out.push(<span key={key++} className="tok-keyword">{word}</span>); } else plain += word;
      at = end;
      continue;
    }
    if (/\d/.test(ch) && !/\w/.test(code[at - 1] ?? " ")) {
      let end = at + 1;
      while (end < code.length && /[\d.]/.test(code[end]!)) end += 1;
      flush();
      out.push(<span key={key++} className="tok-number">{code.slice(at, end)}</span>);
      at = end;
      continue;
    }
    plain += ch;
    at += 1;
  }
  flush();
  return out;
}

function TerminalBar({ label, children }: { label: string; children?: ReactNode }) {
  return (
    <div className="term__bar">
      <span className="term__dots" aria-hidden="true"><i /><i /><i /></span>
      <span className="term__label">{label}</span>
      <span className="term__actions">{children}</span>
    </div>
  );
}

/** A code block in an answer: colours and a copy button; a language XENIO can run gets Run. */
export function CodeBlock({ code, language }: { code: string; language: string }) {
  const name = runnable(language);
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try { await navigator.clipboard.writeText(code); setCopied(true); setTimeout(() => setCopied(false), 1500); } catch { /* the page may not be allowed: nothing is claimed */ }
  };
  if (open && name) return <CodeRunner language={name} code={code} />;
  return (
    <figure className="term term--block">
      <TerminalBar label={name ? LANGUAGE_NAMES[name]! : language || "code"}>
        <button type="button" className="term__btn" onClick={() => void copy()}>{copied ? <Check size={13} aria-hidden="true" /> : <Copy size={13} aria-hidden="true" />} {copied ? "Copied" : "Copy"}</button>
        {name && <button type="button" className="term__btn term__btn--run" onClick={() => setOpen(true)}><Play size={13} aria-hidden="true" /> Try it</button>}
      </TerminalBar>
      <pre className="term__code"><code>{name ? highlight(code, name) : code}</code></pre>
    </figure>
  );
}

export function CodeRunner({ language, code: start, stdin: startInput = "", task, checks, solution, lesson, index, prompt, title }: {
  language: string; code: string; stdin?: string; task?: string; checks?: { name?: string; stdin: string; expected: string }[]; solution?: string; lesson?: number; index?: number; prompt?: string; title?: string;
}) {
  const { app } = useApp();
  const [code, setCode] = useState(start);
  const [input, setInput] = useState(startInput);
  const [run, setRun] = useState<CodeRun | null>(null);
  const [marked, setMarked] = useState<ExerciseResult | null>(null);
  const [busy, setBusy] = useState<"run" | "check" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showSolution, setShowSolution] = useState(false);
  const lines = useMemo(() => code.split("\n").length, [code]);
  const exercise = Boolean(checks?.length);

  const execute = async () => {
    setBusy("run"); setError(null); setMarked(null);
    try {
      const out = await app.api.changeLearning({ action: "run_code", language, code, stdin: input });
      setRun(out.result ?? null);
    } catch (e) { setError((e as Error).message); } finally { setBusy(null); }
  };
  const mark = async () => {
    setBusy("check"); setError(null); setRun(null);
    try {
      setMarked(await app.api.changeLearning({ action: "check_code", lesson, index, language, code }) as unknown as ExerciseResult);
    } catch (e) { setError((e as Error).message); } finally { setBusy(null); }
  };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Tab" && !e.shiftKey) {
      e.preventDefault();
      const el = e.currentTarget, s = el.selectionStart, end = el.selectionEnd;
      const next = `${code.slice(0, s)}    ${code.slice(end)}`;
      setCode(next);
      requestAnimationFrame(() => { el.selectionStart = el.selectionEnd = s + 4; });
    }
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); void execute(); }
    if (e.key === "Escape") e.currentTarget.blur();
  };

  return (
    <div className="activity activity--code">
      {title && <h4 className="activity__title">{title}</h4>}
      {task && <p className="activity__task"><strong>Your task:</strong> {task}</p>}
      {prompt && <p className="activity__prompt">{prompt}</p>}
      <figure className="term">
        <TerminalBar label={`main · ${LANGUAGE_NAMES[language] ?? language}`}>
          <button type="button" className="term__btn" onClick={() => { setCode(start); setRun(null); setMarked(null); }}><RotateCcw size={13} aria-hidden="true" /> Reset</button>
          <button type="button" className="term__btn term__btn--run" disabled={busy !== null} onClick={() => void execute()}><Play size={13} aria-hidden="true" /> {busy === "run" ? "Running…" : "Run"}</button>
        </TerminalBar>
        <div className="term__editor">
          <div className="term__gutter" aria-hidden="true">{Array.from({ length: lines }, (_, i) => <span key={i}>{i + 1}</span>)}</div>
          <div className="term__edit-area">
            <pre className="term__code term__code--under" aria-hidden="true"><code>{highlight(code, language)}{"\n"}</code></pre>
            <textarea aria-label="Your code" value={code} spellCheck={false} autoCapitalize="off" autoCorrect="off" rows={lines} onChange={(e) => setCode(e.target.value)} onKeyDown={onKey} />
          </div>
        </div>
        <label className="term__input"><span>Input</span><textarea aria-label="Input for the program" value={input} rows={Math.max(1, Math.min(5, input.split("\n").length))} spellCheck={false} placeholder="what the program reads, one value a line" onChange={(e) => setInput(e.target.value)} /></label>
        {(run || error || busy) && (
          <div className="term__out" role="status" aria-live="polite">
            <p className="term__prompt"><span>$</span> {RUN_LINE[language] ?? "run"}</p>
            {busy === "run" && <p className="meta">Running in the sandbox…</p>}
            {busy === "check" && <p className="meta">Running your code against the checks…</p>}
            {error && <pre className="term__err">{error}</pre>}
            {run?.stdout && <pre className="term__stdout">{run.stdout}</pre>}
            {run?.stderr && <pre className="term__err">{run.stderr}</pre>}
            {run && !run.stdout && !run.stderr && <p className="meta">The program printed nothing.</p>}
            {run && <p className="term__status">{run.timed_out ? "Stopped: it ran for too long." : run.phase === "compile" ? "It did not compile." : `Exit ${run.exit} · ${run.ms} ms`}</p>}
          </div>
        )}
      </figure>
      <p className="meta term__safe">Runs on this Mac in a sandbox with no network and no access to your files.</p>
      {exercise && (
        <div className="activity__row">
          <button type="button" className="btn btn--primary" disabled={busy !== null} onClick={() => void mark()}><Check size={14} aria-hidden="true" /> {busy === "check" ? "Checking…" : "Check my code"}</button>
          {solution && marked && !marked.all_passed && <button type="button" className="btn btn--quiet" onClick={() => setShowSolution((s) => !s)}><Eye size={14} aria-hidden="true" /> {showSolution ? "Hide the solution" : "Show the solution"}</button>}
        </div>
      )}
      {marked && (
        <div className={`checks ${marked.all_passed ? "checks--ok" : ""}`} role="status">
          <p className="checks__head">{marked.all_passed ? "All checks passed." : `${marked.passed} of ${marked.total} checks passed.`}</p>
          <ul>
            {(marked.results ?? []).map((r, i) => (
              <li key={i} className={r.passed ? "check--ok" : "check--no"}>
                {r.passed ? <Check size={14} aria-label="Passed" /> : <X size={14} aria-label="Failed" />}
                <span>{r.name || `Check ${i + 1}`}{r.stdin ? <code> input: {r.stdin}</code> : null}</span>
                {!r.passed && <div className="check__detail"><p>Expected: <code>{r.expected}</code></p><p>{r.timed_out ? "It ran for too long." : r.error ? <>Error: <code>{r.error}</code></> : <>Got: <code>{r.got || "(nothing)"}</code></>}</p></div>}
              </li>
            ))}
          </ul>
        </div>
      )}
      {showSolution && solution && <figure className="term term--block"><TerminalBar label="A correct solution" /><pre className="term__code"><code>{highlight(solution, language)}</code></pre></figure>}
    </div>
  );
}
