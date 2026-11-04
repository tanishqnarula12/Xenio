import { Maximize2, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type MouseEvent } from "react";

// Diagrams drawn from the UI's own files, loaded the first time one is needed. Mermaid in its strict mode, and what comes back is cleaned once more
// before it reaches the page: a diagram can come from outside content.

type MermaidApi = typeof import("mermaid").default;
let loader: Promise<MermaidApi> | null = null;
let queue: Promise<unknown> = Promise.resolve(); // Mermaid draws one at a time
let counter = 0;

function load(): Promise<MermaidApi> {
  loader ??= import("mermaid").then((module) => {
    module.default.initialize({
      startOnLoad: false, securityLevel: "strict", theme: "base", fontFamily: "-apple-system, BlinkMacSystemFont, 'SF Pro Text', sans-serif",
      flowchart: { curve: "basis", padding: 16, nodeSpacing: 36, rankSpacing: 46, subGraphTitleMargin: { top: 6, bottom: 6 } },
      sequence: { wrap: true, width: 190, height: 46, actorMargin: 60, messageMargin: 34, boxMargin: 8, messageFontSize: 15, actorFontSize: 15, noteFontSize: 14, mirrorActors: false },
      themeVariables: {
        darkMode: true, background: "#16181c", primaryColor: "#1d2a3f", primaryBorderColor: "#7fb2f5", primaryTextColor: "#f5f5f7", secondaryColor: "#1b3029", secondaryBorderColor: "#57c78f",
        secondaryTextColor: "#f5f5f7", tertiaryColor: "#33291a", tertiaryBorderColor: "#e8b84f", tertiaryTextColor: "#f5f5f7", clusterBkg: "#14161a", clusterBorder: "#343a44", titleColor: "#a1a1aa", lineColor: "#8b98ad", textColor: "#f5f5f7", mainBkg: "#1d2a3f", nodeBorder: "#7fb2f5",
        cScale0: "#1d2a3f", cScale1: "#1b3029", cScale2: "#33291a", cScale3: "#2d2240", cScale4: "#3a2226", cScale5: "#1b3038", cScale6: "#2a2f1c", cScale7: "#2b2433", cScale8: "#1d2a3f", cScale9: "#1b3029", cScale10: "#33291a", cScale11: "#2d2240",
        cScaleLabel0: "#f5f5f7", cScaleLabel1: "#f5f5f7", cScaleLabel2: "#f5f5f7", cScaleLabel3: "#f5f5f7", cScaleLabel4: "#f5f5f7", cScaleLabel5: "#f5f5f7", cScaleLabel6: "#f5f5f7", cScaleLabel7: "#f5f5f7", cScaleLabel8: "#f5f5f7", cScaleLabel9: "#f5f5f7", cScaleLabel10: "#f5f5f7", cScaleLabel11: "#f5f5f7",
        edgeLabelBackground: "#24282e", actorBkg: "#1d2a3f", actorBorder: "#7fb2f5", actorTextColor: "#f5f5f7", signalColor: "#a9c8ff", signalTextColor: "#f5f5f7", labelBoxBkgColor: "#24282e", fontSize: "15px",
      },
    });
    return module.default;
  });
  return loader;
}

// the same few looks on every diagram, so a colour always means the same thing. Hex because Mermaid needs it; these are the tokens in tokens.css.
const LOOKS = [
  "classDef root fill:#25395c,stroke:#a9c8ff,stroke-width:2px,color:#ffffff,font-weight:600",
  "classDef key fill:#2f2547,stroke:#c4a7ff,stroke-width:2px,color:#ffffff,font-weight:600",
  "classDef decide fill:#352a14,stroke:#e8b84f,stroke-width:1.5px,color:#fff4d6",
  "classDef good fill:#143029,stroke:#57c78f,stroke-width:1.5px,color:#dff7ea",
  "classDef bad fill:#3a1f23,stroke:#f27474,stroke-width:1.5px,color:#ffe3e3",
  "classDef leaf fill:#1b2b33,stroke:#6fb7cf,stroke-width:1.25px,color:#e6f6fb",
];
const NOT_NODES = /^\s*(?:flowchart|graph|subgraph|end\b|classDef|class\s|style\s|linkStyle|click\s|direction\s|%%)/i;
const ARROW = /\s*(?:<)?(?:--+|==+|-\.+-?)(?:>|o\b|x\b)?(?:\s*\|[^|]*\|)?\s*/;

/** Hierarchy you can see: where it starts, where it branches and where it ends, worked out from the shape of the flowchart. Only for what the writer did not mark themselves. */
export function styleDiagram(source: string): string {
  if (!/^\s*(?:flowchart|graph)\b/i.test(source)) return source;
  const seen: string[] = [];
  const into = new Set<string>(), outOf = new Set<string>(), decisions = new Set<string>(), marked = new Set<string>();
  for (const raw of source.split("\n")) {
    const named = /^\s*class\s+([\w,\s-]+?)\s+\w+\s*;?\s*$/.exec(raw);
    if (named) named[1]!.split(",").forEach((id) => marked.add(id.trim()));
    if (NOT_NODES.test(raw)) continue;
    const line = raw.replace(/"[^"]*"/g, '""');
    for (const hit of line.matchAll(/([\w-]+)(?:\[[^\]]*\]+|\{[^}]*\}+|\([^)]*\)+)?:::\w+/g)) marked.add(hit[1]!);
    for (const hit of line.matchAll(/([\w-]+)\{/g)) decisions.add(hit[1]!);
    const groups = line.split(ARROW).map((part) => part.split("&").map((one) => /^\s*([A-Za-z_][\w-]*)/.exec(one)?.[1]).filter((id): id is string => Boolean(id))).filter((ids) => ids.length);
    groups.forEach((ids, n) => {
      ids.forEach((id) => { if (!seen.includes(id)) seen.push(id); if (n > 0) into.add(id); if (n < groups.length - 1) outOf.add(id); });
    });
  }
  if (seen.length < 2) return source;
  const roots = seen.filter((id) => !into.has(id));
  const start = roots.length ? roots : [seen[0]!]; // a graph that is one big cycle still starts somewhere
  const leaves = seen.filter((id) => !outOf.has(id) && !start.includes(id));
  const assign = (ids: string[], look: string) => { const free = ids.filter((id) => !marked.has(id)); return free.length ? [`class ${free.join(",")} ${look}`] : []; };
  return [source.trimEnd(), ...LOOKS, ...assign(start, "root"), ...assign(seen.filter((id) => decisions.has(id) && !start.includes(id)), "decide"), ...assign(leaves.filter((id) => !decisions.has(id)), "leaf")].join("\n");
}

const REMOVED = "script, iframe, object, embed, a, image, img, video, audio, source, link, meta, base, form, animate, animateTransform, animateMotion, set";

/** Parsed the way the page will parse it (as HTML), because that is what has to be safe. Strict XML refused any <br>, which is every diagram with a two-line box. */
export function cleanSvg(svg: string): string {
  const doc = new DOMParser().parseFromString(svg, "text/html");
  const root = doc.body.querySelector("svg");
  if (!root || !root.children.length) return "";
  root.querySelectorAll(REMOVED).forEach((n) => n.remove());
  // some diagrams always put their labels in HTML: only plain text elements stay in there, without any attribute but its look
  root.querySelectorAll("*").forEach((holder) => {
    if (holder.localName.toLowerCase() !== "foreignobject") return;
    holder.querySelectorAll("*").forEach((node) => {
      const tag = node.localName.toLowerCase();
      if (!["div", "span", "p", "br", "b", "i", "strong", "em", "code", "text"].includes(tag)) { node.replaceWith(doc.createTextNode(node.textContent ?? "")); return; }
      for (const { name } of Array.from(node.attributes)) if (!["class", "style", "xmlns"].includes(name.toLowerCase())) node.removeAttribute(name);
    });
  });
  [root, ...Array.from(root.querySelectorAll("*"))].forEach((node) => {
    for (const { name, value } of Array.from(node.attributes)) {
      const low = name.toLowerCase();
      if (low.startsWith("on") || ((low === "href" || low === "xlink:href") && !value.startsWith("#")) || /javascript:|url\(\s*['"]?(?!#)/i.test(value)) node.removeAttribute(name);
    }
  });
  root.querySelectorAll("style").forEach((s) => { s.textContent = (s.textContent ?? "").replace(/@import[^;]*;?/gi, "").replace(/url\(\s*['"]?(?!#)[^)]*\)/gi, "none"); });
  return root.outerHTML;
}

const NEEDS_QUOTES = /[()[\]{}<>|:;,#&/\\"=+*?!@%]/;
const label = (text: string) => `"${text.trim().replace(/"/g, "#quot;")}"`;

/** What models most often get wrong in a flowchart: a label with brackets or punctuation and no quotes, or quotes inside quotes. Only tried when the drawing as written has failed. */
export function repairDiagram(source: string): string {
  const text = source.replace(/^\s*```(?:mermaid)?\s*\n?/, "").replace(/\n?\s*```\s*$/, "").trim();
  if (!/^\s*(?:flowchart|graph)\b/i.test(text)) return text;
  return text.split("\n").map((line) => line
    // A["say "hi" now"]: the quotes inside would end the label early
    .replace(/(\b[\w-]+)\["(.*?)"\](?=\s*(?:-|=|\.|&|$|:::))/g, (_all, id: string, body: string) => `${id}[${label(body)}]`)
    // A[call f(x)] and B{is x > 1?}: no quotes at all
    .replace(/(\b[\w-]+)\[(?!["[(/\\])([^\]\n]*?)\](?!\])/g, (all, id: string, body: string) => (NEEDS_QUOTES.test(body) ? `${id}[${label(body)}]` : all))
    .replace(/(\b[\w-]+)\{(?!["{])([^}\n]*?)\}(?!\})/g, (all, id: string, body: string) => (NEEDS_QUOTES.test(body) ? `${id}{${label(body)}}` : all))
    // -->|if x > 1| B
    .replace(/\|(?!")([^|\n]+)\|/g, (all, body: string) => (NEEDS_QUOTES.test(body) ? `|${label(body)}|` : all))).join("\n");
}

const squeeze = (text: string) => text.replace(/\s+/g, "").toLowerCase();

export function MermaidView({ source, alt, notes, showSource }: { source: string; alt?: string; notes?: Record<string, string>; showSource?: boolean }) {
  const [svg, setSvg] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const [picked, setPicked] = useState<string | null>(null);
  const [big, setBig] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!big) return;
    const close = (e: KeyboardEvent) => { if (e.key === "Escape") setBig(false); };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [big]);
  const lookup = useMemo(() => new Map(Object.entries(notes ?? {}).map(([k, v]) => [squeeze(k), { label: k, note: v }])), [notes]);

  useEffect(() => {
    let live = true;
    setFailed(false);
    const id = `xenio-diagram-${++counter}`;
    queue = queue.then(async () => {
      const api = await load().catch((e: unknown) => { console.warn("the diagram library did not load", e); return null; });
      const repaired = repairDiagram(source);
      for (const attempt of new Set([styleDiagram(source), source, styleDiagram(repaired), repaired])) {
        try {
          const { svg: drawn } = await api!.render(id, attempt);
          document.getElementById(`d${id}`)?.remove();
          const clean = cleanSvg(drawn);
          if (clean) {
            if (live) { setSvg(clean); setFailed(false); }
            return;
          }
          console.warn("a diagram was drawn, but cleaning it left nothing");
        } catch (e) {
          document.getElementById(`d${id}`)?.remove();
          console.warn("a diagram could not be drawn:", e instanceof Error ? e.message : e);
        }
      }
      if (live) setFailed(true);
    });
    return () => { live = false; };
  }, [source]);

  useEffect(() => {
    const el = box.current;
    if (!el || !lookup.size) return;
    el.querySelectorAll<SVGElement>(".node, .mindmap-node, .section-root, g[class*=section-]").forEach((node) => {
      if (lookup.has(squeeze(node.textContent ?? ""))) node.classList.add("diagram__has-note");
    });
  }, [svg, lookup]);

  const onClick = (e: MouseEvent<HTMLDivElement>) => {
    const node = (e.target as Element).closest?.(".node, .mindmap-node, g[class*=section-]");
    const hit = node ? lookup.get(squeeze(node.textContent ?? "")) : null;
    if (hit) setPicked(hit.label);
  };

  if (failed) {
    if (showSource) return <div className="diagram diagram--failed"><p className="meta">This diagram could not be drawn, so here is what it says:</p><pre className="diagram__source">{source}</pre></div>;
    return <div className="diagram diagram--failed"><p className="meta">This diagram could not be drawn, so here it is in words:</p><p>{alt}</p></div>;
  }
  const pickedNote = picked ? notes?.[picked] : null;
  return (
    <>
      <div className="diagram-frame">
        <div ref={box} className={`diagram ${svg ? "" : "diagram--loading"}`} role="img" aria-label={alt} onClick={onClick}
          dangerouslySetInnerHTML={svg ? { __html: svg } : undefined}>{svg ? undefined : null}</div>
        {svg && <button type="button" className="diagram-frame__zoom" aria-label="See the diagram bigger" title="See it bigger" onClick={() => setBig(true)}><Maximize2 size={14} aria-hidden="true" /></button>}
      </div>
      {!svg && <p className="meta">Drawing the diagram…</p>}
      {big && svg && (
        <div className="diagram-big" role="dialog" aria-modal="true" aria-label={alt ?? "The diagram, bigger"} onClick={(e) => { if (e.target === e.currentTarget) setBig(false); }}>
          <button type="button" className="diagram-big__close" aria-label="Close" autoFocus onClick={() => setBig(false)}><X size={18} aria-hidden="true" /></button>
          <div className="diagram diagram--big" dangerouslySetInnerHTML={{ __html: svg }} />
        </div>
      )}
      {lookup.size > 0 && (
        <div className="diagram-notes">
          <p className="meta">Tap a part to learn more:</p>
          <div className="chips">
            {Object.keys(notes ?? {}).map((label) => (
              <button key={label} type="button" className={`chip ${picked === label ? "chip--on" : ""}`} aria-pressed={picked === label} onClick={() => setPicked(picked === label ? null : label)}>{label}</button>
            ))}
          </div>
          {pickedNote && <p className="diagram-note" role="status"><strong>{picked}.</strong> {pickedNote}</p>}
        </div>
      )}
    </>
  );
}
