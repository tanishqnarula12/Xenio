import { Fragment, type ReactNode } from "react";

// Safe rendering of XENIO's answers (and of anything that came from outside: web pages, mail, reports). The text is
// turned into React elements - never into HTML - so markup in it shows as text and cannot run. Only http(s) links
// become links, and they open in a new tab without a referrer.

const INLINE = /(\*\*[^*\n]+\*\*|`[^`\n]+`|https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g;

export function safeUrl(raw: string): string | null {
  try {
    const url = new URL(raw);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}

function inline(text: string, key: string): ReactNode[] {
  return text.split(INLINE).filter(Boolean).map((part, i) => {
    const k = `${key}-${i}`;
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) return <strong key={k}>{part.slice(2, -2)}</strong>;
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2) return <code key={k}>{part.slice(1, -1)}</code>;
    if (/^https?:\/\//.test(part)) {
      const url = safeUrl(part);
      return url ? <a key={k} href={url} target="_blank" rel="noopener noreferrer">{part}</a> : <Fragment key={k}>{part}</Fragment>;
    }
    return <Fragment key={k}>{part}</Fragment>;
  });
}

export function RichText({ text, className }: { text: string; className?: string }) {
  const blocks: ReactNode[] = [];
  const lines = (text ?? "").replace(/\r\n/g, "\n").split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i]!;
    if (line.startsWith("```")) {
      const code: string[] = [];
      i += 1;
      while (i < lines.length && !lines[i]!.startsWith("```")) { code.push(lines[i]!); i += 1; }
      i += 1;
      blocks.push(<pre key={`b${blocks.length}`}><code>{code.join("\n")}</code></pre>);
      continue;
    }
    if (/^\s*[-*•]\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const items: string[] = [];
      while (i < lines.length && (ordered ? /^\s*\d+[.)]\s+/ : /^\s*[-*•]\s+/).test(lines[i]!)) {
        items.push(lines[i]!.replace(/^\s*(?:[-*•]|\d+[.)])\s+/, ""));
        i += 1;
      }
      const List = ordered ? "ol" : "ul";
      blocks.push(<List key={`b${blocks.length}`}>{items.map((it, n) => <li key={n}>{inline(it, `l${n}`)}</li>)}</List>);
      continue;
    }
    if (!line.trim()) { i += 1; continue; }
    const para: string[] = [];
    while (i < lines.length && lines[i]!.trim() && !lines[i]!.startsWith("```") && !/^\s*(?:[-*•]|\d+[.)])\s+/.test(lines[i]!)) {
      para.push(lines[i]!.replace(/^#{1,6}\s+/, ""));
      i += 1;
    }
    blocks.push(<p key={`b${blocks.length}`}>{inline(para.join(" "), `p${blocks.length}`)}</p>);
  }
  return <div className={`rich ${className ?? ""}`}>{blocks}</div>;
}
