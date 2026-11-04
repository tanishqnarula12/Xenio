import { Fragment, type ReactNode } from "react";
import { CodeBlock } from "../teach/code";
import { MermaidView } from "../teach/Mermaid";
import { QuizBlock } from "../teach/Quiz";

// Safe rendering of XENIO's answers (and of anything that came from outside: web pages, mail, reports). The text is
// turned into React elements - never into HTML - so markup in it shows as text and cannot run. Only http(s) links
// become links, and they open in a new tab without a referrer.

const INLINE = /((?<![\w$])\$(?=[^\s$\d\\]|\d*[A-Za-z\\])[^$\n]*?[^\s$]\$(?![\d\w])|\*\*[^*\n]+\*\*|(?<![\w*])\*(?=[^\s*])[^*\n]*?[^\s*]\*(?![\w*])|`[^`\n]+`|https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g;

export function safeUrl(raw: string): string | null {
  try {
    const url = new URL(raw);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}

const SYMBOLS: Record<string, string> = { rightarrow: "→", to: "→", leftarrow: "←", leftrightarrow: "↔", Rightarrow: "⇒", times: "×", cdot: "·", div: "÷", pm: "±", le: "≤", leq: "≤", ge: "≥", geq: "≥", ne: "≠", neq: "≠", approx: "≈", infty: "∞", pi: "π", theta: "θ", alpha: "α", beta: "β", gamma: "γ", delta: "δ", Delta: "Δ", lambda: "λ", mu: "μ", sigma: "σ", omega: "ω", sum: "Σ", sqrt: "√", degree: "°", circ: "°" };

/** A formula a model wrote in LaTeX, as readable text: subscripts, superscripts and symbols. Only ever text: nothing in it can run. */
function formula(source: string, key: string): ReactNode {
  const text = source
    .replace(/\\xrightarrow\{\\text\{([^}]*)\}\}/g, " →($1) ").replace(/\\xrightarrow\{([^}]*)\}/g, " →($1) ")
    .replace(/\\frac\{([^}]*)\}\{([^}]*)\}/g, "($1)/($2)").replace(/\\(?:text|mathrm|mathbf)\{([^}]*)\}/g, "$1")
    .replace(/\\([A-Za-z]+)/g, (whole, name: string) => SYMBOLS[name] ?? whole).replace(/\\[ ,;!]/g, " ");
  const parts = text.split(/([_^](?:\{[^}]*\}|[A-Za-z0-9+\-]))/).filter(Boolean);
  return (
    <span key={key} className="rich__formula">
      {parts.map((part, i) => {
        const mark = /^[_^]/.test(part) ? part[0] : "";
        if (!mark) return <Fragment key={i}>{part}</Fragment>;
        const body = part.slice(1).replace(/^\{|\}$/g, "");
        return mark === "_" ? <sub key={i}>{body}</sub> : <sup key={i}>{body}</sup>;
      })}
    </span>
  );
}

function inline(text: string, key: string): ReactNode[] {
  return text.split(INLINE).filter(Boolean).map((part, i) => {
    const k = `${key}-${i}`;
    if (part.length > 2 && part.startsWith("$") && part.endsWith("$")) return formula(part.slice(1, -1), k);
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) return <strong key={k}>{part.slice(2, -2)}</strong>;
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2) return <code key={k}>{part.slice(1, -1)}</code>;
    if (part.startsWith("*") && part.endsWith("*") && part.length > 2) return <em key={k}>{part.slice(1, -1)}</em>;
    if (/^https?:\/\//.test(part)) {
      const url = safeUrl(part);
      return url ? <a key={k} href={url} target="_blank" rel="noopener noreferrer">{part}</a> : <Fragment key={k}>{part}</Fragment>;
    }
    return <Fragment key={k}>{part}</Fragment>;
  });
}

/** One line of a lesson, with its **bold**, *emphasis* and `code` shown instead of its asterisks. */
export function Inline({ text }: { text?: string | null }) {
  return <>{inline(text ?? "", "i")}</>;
}

const HEADING = /^(#{1,6})\s+(.*\S)\s*$/;
const RULE = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;
const QUOTE = /^\s*>\s?/;
const TABLE_RULE = /^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$/;
const LIST = /^\s*(?:[-*•]|\d+[.)])\s+/;

function cellsOf(line: string): string[] {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
}

function isTable(lines: string[], i: number): boolean {
  return lines[i]!.includes("|") && i + 1 < lines.length && lines[i + 1]!.includes("-") && TABLE_RULE.test(lines[i + 1]!);
}

export function RichText({ text, className }: { text: string; className?: string }) {
  const blocks: ReactNode[] = [];
  const lines = (text ?? "").replace(/\r\n/g, "\n").split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i]!;
    if (line.startsWith("```")) {
      const language = line.slice(3).trim().toLowerCase();
      const code: string[] = [];
      i += 1;
      while (i < lines.length && !lines[i]!.startsWith("```")) { code.push(lines[i]!); i += 1; }
      const closed = i < lines.length;
      i += 1;
      const body = code.join("\n");
      blocks.push(language === "mermaid"
        ? (closed ? <MermaidView key={`b${blocks.length}`} source={body} alt="A diagram from the answer" showSource /> : <p key={`b${blocks.length}`} className="meta">Drawing the diagram…</p>)
        : language === "quiz"
          ? <QuizBlock key={`b${blocks.length}`} source={body} finished={closed} />
          : <CodeBlock key={`b${blocks.length}`} code={body} language={language} />);
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      const Tag = heading[1]!.length <= 2 ? "h3" : "h4";
      blocks.push(<Tag key={`b${blocks.length}`} className="rich__heading">{inline(heading[2]!, `h${blocks.length}`)}</Tag>);
      i += 1;
      continue;
    }
    if (RULE.test(line)) { blocks.push(<hr key={`b${blocks.length}`} className="rich__rule" />); i += 1; continue; }
    if (isTable(lines, i)) {
      const head = cellsOf(line);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && lines[i]!.includes("|") && lines[i]!.trim()) { rows.push(cellsOf(lines[i]!)); i += 1; }
      const key = blocks.length;
      blocks.push(
        <div key={`b${key}`} className="compare-wrap"><table className="compare">
          <thead><tr>{head.map((cell, n) => <th key={n} scope="col">{inline(cell, `t${key}-${n}`)}</th>)}</tr></thead>
          <tbody>{rows.map((row, r) => <tr key={r}>{row.map((cell, n) => (n === 0 ? <th key={n} scope="row">{inline(cell, `t${key}-${r}-${n}`)}</th> : <td key={n}>{inline(cell, `t${key}-${r}-${n}`)}</td>))}</tr>)}</tbody>
        </table></div>,
      );
      continue;
    }
    if (QUOTE.test(line)) {
      const said: string[] = [];
      while (i < lines.length && QUOTE.test(lines[i]!)) { said.push(lines[i]!.replace(QUOTE, "")); i += 1; }
      blocks.push(<blockquote key={`b${blocks.length}`} className="rich__quote">{inline(said.join(" "), `q${blocks.length}`)}</blockquote>);
      continue;
    }
    if (/^\s*[-*•]\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const items: string[] = [];
      while (i < lines.length && (ordered ? /^\s*\d+[.)]\s+/ : /^\s*[-*•]\s+/).test(lines[i]!)) {
        items.push(lines[i]!.replace(/^\s*(?:[-*•]|\d+[.)])\s+/, ""));
        i += 1;
      }
      // numbered steps with a blank line between them are still steps 1, 2, 3
      const start = ordered ? Number(/\d+/.exec(line)?.[0] ?? 1) : undefined;
      blocks.push(ordered
        ? <ol key={`b${blocks.length}`} start={start}>{items.map((it, n) => <li key={n}>{inline(it, `l${n}`)}</li>)}</ol>
        : <ul key={`b${blocks.length}`}>{items.map((it, n) => <li key={n}>{inline(it, `l${n}`)}</li>)}</ul>);
      continue;
    }
    if (!line.trim()) { i += 1; continue; }
    const para: string[] = [];
    while (i < lines.length && lines[i]!.trim() && !lines[i]!.startsWith("```") && !LIST.test(lines[i]!) && !HEADING.test(lines[i]!) && !RULE.test(lines[i]!) && !QUOTE.test(lines[i]!) && !isTable(lines, i)) {
      para.push(lines[i]!);
      i += 1;
    }
    blocks.push(<p key={`b${blocks.length}`}>{inline(para.join(" "), `p${blocks.length}`)}</p>);
  }
  return <div className={`rich ${className ?? ""}`}>{blocks}</div>;
}
