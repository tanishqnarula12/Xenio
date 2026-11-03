// The formulas in graphs and calculators. A small reader of its own, the same grammar as app/teacher/activities.py: a lesson's formula is
// data, and is never run as code.

export class ExpressionError extends Error {}
type Fn = (v: Record<string, number>) => number;

const ONE: Record<string, (x: number) => number> = {
  sin: Math.sin, cos: Math.cos, tan: Math.tan, asin: Math.asin, acos: Math.acos, atan: Math.atan, sinh: Math.sinh, cosh: Math.cosh, tanh: Math.tanh,
  sqrt: Math.sqrt, cbrt: Math.cbrt, abs: Math.abs, ln: Math.log, log: Math.log10, log10: Math.log10, log2: Math.log2, exp: Math.exp, floor: Math.floor, ceil: Math.ceil, round: Math.round,
};
const MANY: Record<string, (...x: number[]) => number> = { min: Math.min, max: Math.max, pow: Math.pow };
const TOKEN = /\s*(\d+\.?\d*(?:[eE][-+]?\d+)?|\.\d+|[A-Za-z_][A-Za-z_0-9]*|[-+*/^(),%])/y;

function tokens(source: string): string[] {
  const text = source.replace(/×|·/g, "*").replace(/÷/g, "/").replace(/−/g, "-").replace(/²/g, "^2").replace(/³/g, "^3").replace(/π/g, "pi").replace(/√/g, "sqrt");
  const out: string[] = [];
  let at = 0;
  while (at < text.length) {
    if (!text.slice(at).trim()) break;
    TOKEN.lastIndex = at;
    const m = TOKEN.exec(text);
    if (!m) throw new ExpressionError(`cannot read "${text.slice(at, at + 10)}"`);
    out.push(m[1]!);
    at = TOKEN.lastIndex;
  }
  return out;
}

export function compile(source: string): Fn {
  const list = tokens(source);
  let at = 0;
  const peek = () => list[at];
  const take = () => { if (at >= list.length) throw new ExpressionError("it ends too soon"); return list[at++]!; };
  const expression = (): Fn => {
    let left = term();
    while (peek() === "+" || peek() === "-") {
      const plus = take() === "+", a = left, b = term();
      left = plus ? (v) => a(v) + b(v) : (v) => a(v) - b(v);
    }
    return left;
  };
  const term = (): Fn => {
    let left = unary();
    for (;;) {
      const t = peek();
      if (t === "*" || t === "/" || t === "%") {
        take();
        const a = left, b = unary();
        left = t === "*" ? (v) => a(v) * b(v) : t === "/" ? (v) => a(v) / b(v) : (v) => a(v) % b(v);
      } else if (t !== undefined && /^[(A-Za-z_\d.]/.test(t)) { // "2x", "3(x + 1)"
        const a = left, b = unary();
        left = (v) => a(v) * b(v);
      } else return left;
    }
  };
  const unary = (): Fn => {
    if (peek() === "-") { take(); const a = unary(); return (v) => -a(v); }
    if (peek() === "+") { take(); return unary(); }
    return power();
  };
  const power = (): Fn => {
    const base = atom();
    if (peek() === "^") { take(); const e = unary(); return (v) => Math.pow(base(v), e(v)); }
    return base;
  };
  const atom = (): Fn => {
    const t = take();
    if (t === "(") {
      const inner = expression();
      if (take() !== ")") throw new ExpressionError("a bracket is not closed");
      return inner;
    }
    if (/^[\d.]/.test(t)) { const n = Number(t); return () => n; }
    if (t in ONE || t in MANY) {
      if (take() !== "(") throw new ExpressionError(`${t} needs brackets`);
      const args = [expression()];
      while (peek() === ",") { take(); args.push(expression()); }
      if (take() !== ")") throw new ExpressionError("a bracket is not closed");
      if (t in ONE) { const f = ONE[t]!, a = args[0]!; return (v) => f(a(v)); }
      const f = MANY[t]!;
      return (v) => f(...args.map((a) => a(v)));
    }
    return (v) => {
      if (t in v) return v[t]!;
      if (t === "pi") return Math.PI;
      if (t === "e") return Math.E;
      throw new ExpressionError(`unknown name "${t}"`);
    };
  };
  const whole = expression();
  if (at < list.length) throw new ExpressionError(`unexpected "${list[at]}"`);
  return whole;
}

export function format(value: number, digits = 3): string {
  if (!Number.isFinite(value)) return "—";
  const rounded = Number(value.toFixed(Math.max(0, Math.min(8, digits))));
  return Math.abs(rounded) >= 1e7 || (rounded !== 0 && Math.abs(rounded) < 1e-4) ? value.toExponential(2) : rounded.toLocaleString("en-US", { maximumFractionDigits: digits });
}
