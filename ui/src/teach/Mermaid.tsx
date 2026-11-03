import { useEffect, useMemo, useRef, useState, type MouseEvent } from "react";

// Diagrams drawn from the UI's own files, loaded the first time one is needed. Text in the SVG only (no HTML labels), Mermaid in its strict mode, and
// what comes back is cleaned once more before it reaches the page: a diagram can come from outside content.

type MermaidApi = typeof import("mermaid").default;
let loader: Promise<MermaidApi> | null = null;
let queue: Promise<unknown> = Promise.resolve(); // Mermaid draws one at a time
let counter = 0;

function load(): Promise<MermaidApi> {
  loader ??= import("mermaid").then((module) => {
    module.default.initialize({
      startOnLoad: false, securityLevel: "strict", theme: "base", fontFamily: "-apple-system, BlinkMacSystemFont, 'SF Pro Text', sans-serif",
      flowchart: { htmlLabels: false, curve: "basis", padding: 12, nodeSpacing: 30, rankSpacing: 36 },
      sequence: { wrap: true, width: 190, height: 46, actorMargin: 60, messageMargin: 34, boxMargin: 8, messageFontSize: 15, actorFontSize: 15, noteFontSize: 14, mirrorActors: false },
      themeVariables: {
        darkMode: true, background: "#16181c", primaryColor: "#1d2a3f", primaryBorderColor: "#7fb2f5", primaryTextColor: "#f5f5f7", secondaryColor: "#1b3029", secondaryBorderColor: "#57c78f",
        secondaryTextColor: "#f5f5f7", tertiaryColor: "#33291a", tertiaryBorderColor: "#e8b84f", tertiaryTextColor: "#f5f5f7", lineColor: "#a9c8ff", textColor: "#f5f5f7", mainBkg: "#1d2a3f", nodeBorder: "#7fb2f5",
        cScale0: "#1d2a3f", cScale1: "#1b3029", cScale2: "#33291a", cScale3: "#2d2240", cScale4: "#3a2226", cScale5: "#1b3038", cScale6: "#2a2f1c", cScale7: "#2b2433", cScale8: "#1d2a3f", cScale9: "#1b3029", cScale10: "#33291a", cScale11: "#2d2240",
        cScaleLabel0: "#f5f5f7", cScaleLabel1: "#f5f5f7", cScaleLabel2: "#f5f5f7", cScaleLabel3: "#f5f5f7", cScaleLabel4: "#f5f5f7", cScaleLabel5: "#f5f5f7", cScaleLabel6: "#f5f5f7", cScaleLabel7: "#f5f5f7", cScaleLabel8: "#f5f5f7", cScaleLabel9: "#f5f5f7", cScaleLabel10: "#f5f5f7", cScaleLabel11: "#f5f5f7",
        edgeLabelBackground: "#24282e", actorBkg: "#1d2a3f", actorBorder: "#7fb2f5", actorTextColor: "#f5f5f7", signalColor: "#a9c8ff", signalTextColor: "#f5f5f7", labelBoxBkgColor: "#24282e", fontSize: "15px",
      },
    });
    return module.default;
  });
  return loader;
}

export function cleanSvg(svg: string): string {
  const doc = new DOMParser().parseFromString(svg, "image/svg+xml");
  if (doc.querySelector("parsererror")) return "";
  doc.querySelectorAll("script, iframe, object, embed, a, image, animate, set").forEach((n) => n.remove());
  // some diagrams (mind maps) always put their labels in HTML: only plain text elements stay in there, without any attribute but its look
  doc.querySelectorAll("foreignObject *").forEach((node) => {
    const tag = node.localName.toLowerCase();
    if (!["div", "span", "p", "br", "b", "i", "strong", "em", "code", "text"].includes(tag)) { node.replaceWith(doc.createTextNode(node.textContent ?? "")); return; }
    for (const { name } of Array.from(node.attributes)) if (!["class", "style", "xmlns"].includes(name.toLowerCase())) node.removeAttribute(name);
  });
  doc.querySelectorAll("*").forEach((node) => {
    for (const { name, value } of Array.from(node.attributes)) {
      const low = name.toLowerCase();
      if (low.startsWith("on") || ((low === "href" || low === "xlink:href") && !value.startsWith("#")) || /javascript:|url\(\s*['"]?(?!#)/i.test(value)) node.removeAttribute(name);
    }
  });
  doc.querySelectorAll("style").forEach((s) => { s.textContent = (s.textContent ?? "").replace(/@import[^;]*;?/gi, "").replace(/url\(\s*['"]?(?!#)[^)]*\)/gi, "none"); });
  return new XMLSerializer().serializeToString(doc.documentElement);
}

const squeeze = (text: string) => text.replace(/\s+/g, "").toLowerCase();

export function MermaidView({ source, alt, notes }: { source: string; alt?: string; notes?: Record<string, string> }) {
  const [svg, setSvg] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const [picked, setPicked] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const lookup = useMemo(() => new Map(Object.entries(notes ?? {}).map(([k, v]) => [squeeze(k), { label: k, note: v }])), [notes]);

  useEffect(() => {
    let live = true;
    setFailed(false);
    const id = `xenio-diagram-${++counter}`;
    queue = queue.then(async () => {
      try {
        const api = await load();
        const { svg: drawn } = await api.render(id, source);
        document.getElementById(`d${id}`)?.remove();
        if (live) { const clean = cleanSvg(drawn); setSvg(clean); setFailed(!clean); }
      } catch {
        document.getElementById(`d${id}`)?.remove();
        if (live) setFailed(true);
      }
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
    return <div className="diagram diagram--failed"><p className="meta">This diagram could not be drawn, so here it is in words:</p><p>{alt}</p></div>;
  }
  const pickedNote = picked ? notes?.[picked] : null;
  return (
    <>
      <div ref={box} className={`diagram ${svg ? "" : "diagram--loading"}`} role="img" aria-label={alt} onClick={onClick}
        dangerouslySetInnerHTML={svg ? { __html: svg } : undefined}>{svg ? undefined : null}</div>
      {!svg && <p className="meta">Drawing the diagram…</p>}
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
