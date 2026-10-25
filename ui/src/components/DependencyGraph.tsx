import type { RunTask } from "../api/types";
import { StatusOf } from "./ui";

const W = 210, H = 70, GX = 56, GY = 14;

export interface Placed { task: RunTask; col: number; row: number }

/** Columns by dependency depth (a task sits right of everything it waits for); rows in plan order. */
export function layout(tasks: RunTask[]): Placed[] {
  const byId = new Map(tasks.map((t) => [t.id, t]));
  const depth = new Map<string, number>();
  const visit = (id: string, seen: Set<string>): number => {
    if (depth.has(id)) return depth.get(id)!;
    if (seen.has(id)) return 0; // a cycle is refused by XENIO's planner; never loop here
    seen.add(id);
    const deps = byId.get(id)?.depends_on.filter((d) => byId.has(d)) ?? [];
    const d = deps.length ? Math.max(...deps.map((x) => visit(x, seen))) + 1 : 0;
    depth.set(id, d);
    return d;
  };
  tasks.forEach((t) => visit(t.id, new Set()));
  const rows = new Map<number, number>();
  return tasks.map((task) => {
    const col = depth.get(task.id) ?? 0;
    const row = rows.get(col) ?? 0;
    rows.set(col, row + 1);
    return { task, col, row };
  });
}

/** The plan as a graph. The same facts are in the task table below it, which is what screen readers and the keyboard
 *  use (a click on a node is a shortcut for the table's "Details"). */
export function DependencyGraph({ tasks, selected, onSelect }: { tasks: RunTask[]; selected?: string | null;
  onSelect?: (id: string) => void }) {
  if (!tasks.length) return null;
  const placed = layout(tasks);
  const pos = new Map(placed.map((p) => [p.task.id, p]));
  const cols = Math.max(...placed.map((p) => p.col)) + 1;
  const rows = Math.max(...placed.map((p) => p.row)) + 1;
  const width = cols * W + (cols - 1) * GX, height = rows * H + (rows - 1) * GY;
  const x = (c: number) => c * (W + GX), y = (r: number) => r * (H + GY);
  return (
    <div className="graph-scroll">
      <div className="graph" style={{ width, height }} aria-hidden="true">
        <svg width={width} height={height} className="graph__edges">
          {placed.flatMap((p) => p.task.depends_on.filter((d) => pos.has(d)).map((d) => {
            const from = pos.get(d)!;
            const x1 = x(from.col) + W, y1 = y(from.row) + H / 2, x2 = x(p.col), y2 = y(p.row) + H / 2;
            const mid = (x1 + x2) / 2;
            const done = from.task.status === "succeeded";
            return <path key={`${d}-${p.task.id}`} d={`M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`}
              className={done ? "edge edge--done" : "edge"} />;
          }))}
        </svg>
        {placed.map((p) => (
          <div key={p.task.id} className={`graph__node graph__node--${p.task.status} ${selected === p.task.id ? "graph__node--selected" : ""}`}
            style={{ left: x(p.col), top: y(p.row), width: W, height: H, cursor: onSelect ? "pointer" : undefined }}
            onClick={onSelect ? () => onSelect(p.task.id) : undefined}>
            <p className="graph__title">{p.task.title}</p>
            <p className="graph__meta"><span className="meta">{p.task.role}</span><StatusOf value={p.task.status} /></p>
          </div>
        ))}
      </div>
    </div>
  );
}
