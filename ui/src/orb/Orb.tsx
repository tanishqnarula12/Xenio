import { useEffect, useRef, useState } from "react";
import type { OrbState } from "../api/types";
import { lookFor, OrbRenderer } from "./renderer";

export interface OrbProps {
  state: OrbState;
  label: string;
  size: number; // CSS pixels
  detail?: "auto" | "low" | "high";
  reducedMotion: boolean;
  level?: (() => number) | null; // real audio only
  agents?: number;
  className?: string;
  /** the particles react to the mouse (the big orb on Home) */
  interactive?: boolean;
}

function particlesFor(size: number, detail: "auto" | "low" | "high"): number {
  if (detail === "low") return size > 160 ? 2600 : 700;
  if (detail === "high") return size > 160 ? 11000 : 2400;
  return size > 160 ? 7000 : 1400;
}

/** The orb. Its meaning is always in words too (role="img" + label), so nothing depends on seeing it move. */
export function Orb({ state, label, size, detail = "auto", reducedMotion, level = null, agents = 2, className, interactive = false }: OrbProps) {
  const halo = useRef<HTMLCanvasElement>(null);
  const dots = useRef<HTMLCanvasElement>(null);
  const renderer = useRef<OrbRenderer | null>(null);
  const [drawable, setDrawable] = useState(true);

  useEffect(() => {
    if (!halo.current || !dots.current) return;
    const orb = new OrbRenderer(halo.current, dots.current, { particles: particlesFor(size, detail), reducedMotion, interactive });
    renderer.current = orb;
    setDrawable(orb.drawable);
    orb.setState(state);
    orb.start();
    const observer = typeof ResizeObserver === "function" ? new ResizeObserver(() => orb.resize()) : null;
    observer?.observe(dots.current);
    return () => {
      observer?.disconnect();
      orb.destroy();
      renderer.current = null;
    };
    // the renderer is created once per mount; later changes are passed in below
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => { renderer.current?.setState(state); }, [state]);
  useEffect(() => { renderer.current?.setOptions({ particles: particlesFor(size, detail), reducedMotion, interactive }); },
    [size, detail, reducedMotion, interactive]);
  useEffect(() => { renderer.current?.setLevelSource(level); }, [level]);
  useEffect(() => { renderer.current?.setAgents(agents); }, [agents]);

  const [r, g, b] = lookFor(state).color;
  return (
    <div className={`orb ${size >= 100 ? "orb--glow" : ""} ${className ?? ""}`} style={{ width: size, height: size, color: `rgb(${r},${g},${b})` }} role="img"
      aria-label={`XENIO: ${label}`} data-state={state}>
      <canvas ref={halo} className="orb__layer" aria-hidden="true" />
      <canvas ref={dots} className="orb__layer" aria-hidden="true" />
      {!drawable && (
        <span className="orb__still" aria-hidden="true"
          style={{ background: `radial-gradient(circle at 50% 45%, rgba(${r},${g},${b},0.95) 0 18%, rgba(${r},${g},${b},0.28) 42%, rgba(${r},${g},${b},0) 70%)` }} />
      )}
    </div>
  );
}
