import { useReducedMotion } from "../motion/motion";

/**
 * XENIO's picture being made: its orb with a line of light that winds around it, gathering points of light as it goes, like something small and curious finding
 * its way. It says nothing about how far along the work is, because the provider does not report that: the state in words below it is the honest part.
 * CSS only and on the compositor. It leaves the page when the job ends, and stands still for those who ask for reduced motion.
 */
const PATH = "M60 22 H140 Q178 22 178 60 V140 Q178 178 140 178 H60 Q22 178 22 140 V60 Q22 22 60 22 Z";
const POINTS: [number, number][] = [[100, 22], [178, 100], [100, 178], [22, 100], [156, 30], [44, 170]];

export function GenerationMark({ state }: { state: string }) {
  const reduced = useReducedMotion();
  return (
    <div className={`genmark genmark--${state} ${reduced ? "genmark--still" : ""}`} aria-hidden="true">
      <div className="genmark__orb"><span /><span /><span /></div>
      <svg viewBox="0 0 200 200" className="genmark__path">
        <path d={PATH} className="genmark__track" />
        <path d={PATH} className="genmark__snake" pathLength={100} />
        {POINTS.map(([x, y], i) => <circle key={i} cx={x} cy={y} r="2.6" className="genmark__point" style={{ animationDelay: `${(i * 3.2) / POINTS.length}s` }} />)}
      </svg>
    </div>
  );
}
