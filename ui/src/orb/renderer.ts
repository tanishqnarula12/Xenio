// XENIO's orb: a sphere of light particles whose shape, colour and motion say what XENIO is doing.
//
// The particle technique (a Fibonacci sphere of point sprites drawn with WebGL, shapes blended by weights, a soft
// radial halo, loudness from a Web Audio analyser) is adapted from Ship Notes' "speaking-orb"
// (https://github.com/aqualang89/shipnotes-components, MIT License, Copyright (c) 2026 Ship Notes; see
// ui/THIRD_PARTY_NOTICES.md). The sixteen states, their shapes (lattice, constellation, scan), the palette and the
// GSAP-driven transitions are XENIO's own.
//
// Rules: the orb never invents activity. Its loudness comes from real audio (the microphone while listening, the TTS
// playback while speaking) or is zero. It draws nothing while off-screen or while the tab is hidden, and with reduced
// motion it draws one still frame per state. The pointer is play, not information: near the orb the particles part
// around it and light up, the sphere turns a little towards it, and a click sends a ripple through it.

import { gsap } from "gsap";
import type { OrbState } from "../api/types";

type RGB = [number, number, number];

interface Look {
  forms: number[]; // weights: sphere, knot, bands, speak, done, lattice, constellation
  color: RGB;
  spin: number; // revolutions-ish per second
  wave: number; // surface ripple
  radius: number; // 1 = full size
  alpha: number;
  glow: number;
  pace: number; // time scale (0 = frozen)
  scan: number; // executing: a lit meridian sweeping round
  breathe: number; // waiting / connecting: slow radius swell
  jitter: number; // error: small irregular unrest
}

const F = (i: number) => [0, 1, 2, 3, 4, 5, 6].map((k) => (k === i ? 1 : 0));
const XENON: RGB = [169, 200, 255];
const WARM: RGB = [255, 222, 190];
const LOOKS: Record<OrbState, Look> = {
  DISCONNECTED: { forms: F(0), color: [96, 101, 110], spin: 0, wave: 0, radius: 0.62, alpha: 0.45, glow: 0.02, pace: 0, scan: 0, breathe: 0, jitter: 0 },
  CONNECTING: { forms: F(0), color: [140, 160, 196], spin: 0.05, wave: 2, radius: 0.8, alpha: 0.7, glow: 0.05, pace: 0.6, scan: 0, breathe: 0.06, jitter: 0 },
  INITIALIZING: { forms: F(0), color: [160, 188, 236], spin: 0.08, wave: 4, radius: 0.9, alpha: 0.8, glow: 0.07, pace: 0.8, scan: 0, breathe: 0.03, jitter: 0 },
  IDLE: { forms: F(0), color: XENON, spin: 0.07, wave: 3, radius: 1, alpha: 0.86, glow: 0.07, pace: 0.7, scan: 0, breathe: 0, jitter: 0 },
  LISTENING: { forms: F(0), color: WARM, spin: 0.09, wave: 7, radius: 1.02, alpha: 0.95, glow: 0.09, pace: 1, scan: 0, breathe: 0, jitter: 0 },
  PROCESSING: { forms: F(0), color: [190, 214, 255], spin: 0.22, wave: 5, radius: 0.94, alpha: 0.95, glow: 0.09, pace: 1.2, scan: 0, breathe: 0, jitter: 0 },
  THINKING: { forms: F(1), color: [150, 182, 255], spin: 0.12, wave: 0, radius: 1, alpha: 0.95, glow: 0.08, pace: 1, scan: 0, breathe: 0, jitter: 0 },
  SEARCHING: { forms: F(2), color: [118, 214, 232], spin: 0.1, wave: 0, radius: 1, alpha: 0.95, glow: 0.08, pace: 1, scan: 0, breathe: 0, jitter: 0 },
  PLANNING: { forms: F(5), color: [186, 206, 246], spin: 0.06, wave: 0, radius: 1, alpha: 0.95, glow: 0.07, pace: 0.8, scan: 0, breathe: 0, jitter: 0 },
  ORCHESTRATING: { forms: F(6), color: [142, 210, 230], spin: 0.08, wave: 0, radius: 1, alpha: 0.95, glow: 0.08, pace: 0.9, scan: 0, breathe: 0, jitter: 0 },
  EXECUTING: { forms: F(0), color: [127, 178, 245], spin: 0.1, wave: 2, radius: 0.98, alpha: 0.92, glow: 0.08, pace: 1, scan: 1, breathe: 0, jitter: 0 },
  SPEAKING: { forms: F(3), color: WARM, spin: 0.08, wave: 0, radius: 1, alpha: 0.95, glow: 0.08, pace: 1, scan: 0, breathe: 0, jitter: 0 },
  WAITING_APPROVAL: { forms: F(0), color: [232, 184, 79], spin: 0.04, wave: 2, radius: 0.96, alpha: 0.95, glow: 0.11, pace: 0.8, scan: 0, breathe: 0.05, jitter: 0 },
  PAUSED: { forms: F(0), color: [120, 128, 140], spin: 0, wave: 0, radius: 0.92, alpha: 0.6, glow: 0.03, pace: 0, scan: 0, breathe: 0, jitter: 0 },
  COMPLETED: { forms: F(4), color: [87, 199, 143], spin: 0, wave: 0, radius: 1, alpha: 0.95, glow: 0.08, pace: 1, scan: 0, breathe: 0, jitter: 0 },
  ERROR: { forms: F(0), color: [242, 116, 116], spin: 0.03, wave: 1, radius: 0.9, alpha: 0.9, glow: 0.09, pace: 0.7, scan: 0, breathe: 0, jitter: 1 },
};

export function lookFor(state: OrbState): Readonly<Look> {
  return LOOKS[state];
}

interface Seed { x: number; y: number; z: number; a: number; u: number; sa: number; ca: number }

const TAU = Math.PI * 2;
const seedCache = new Map<number, Seed[]>();
function seeds(count: number): Seed[] {
  const cached = seedCache.get(count);
  if (cached) return cached;
  // a dense Fibonacci sphere shows moire, so past 1000 points each one gets a small fixed nudge
  const jitter = count > 1000 ? ((1 - 1000 / count) * 1.6) / Math.sqrt(count) : 0;
  const rnd = (n: number) => { const x = Math.sin(n * 12.9898) * 43758.5453; return x - Math.floor(x); };
  const out = Array.from({ length: count }, (_, i) => {
    const y0 = 1 - (2 * (i + 0.5)) / count;
    const a = i * 2.399963229728653 + (rnd(i) - 0.5) * jitter * 6;
    const y = Math.max(-1, Math.min(1, y0 + (rnd(i + 0.37) - 0.5) * jitter));
    const r = Math.sqrt(1 - y * y);
    return { x: Math.cos(a) * r, y, z: Math.sin(a) * r, a, u: i / count, sa: Math.sin(a), ca: Math.cos(a) };
  });
  seedCache.set(count, out);
  return out;
}
const BAND_TILT = [[0.32, 0], [-0.72, 1.02], [1.08, -0.82]].map(([t, r]) =>
  [Math.cos(t! + 1), Math.sin(t! + 1), Math.cos(r!), Math.sin(r!)] as const);

const VS = `attribute vec2 p;attribute vec3 d;uniform vec2 res;varying float a;varying float s;
void main(){float size=max(d.x,2.0);float k=min(1.0,d.x*d.x/(size*size));a=d.y*k;s=d.z*k;
gl_Position=vec4(p/res*2.0-1.0,0.0,1.0)*vec4(1,-1,1,1);gl_PointSize=size;}`;
const FS = `precision mediump float;uniform vec3 col;varying float a;varying float s;
void main(){float r=length(gl_PointCoord-.5)*2.0;if(r>1.0)discard;
float core=mix(1.0,.06,smoothstep(.16,.24,r))*(1.0-smoothstep(.88,1.0,r));
float spark=mix(1.0,.06,smoothstep(.07,.12,r))*(1.0-smoothstep(.16,.2,r));
gl_FragColor=vec4(col*core*a+vec3(spark*s),min(1.0,core*a+spark*s));}`;

interface GL { gl: WebGLRenderingContext; res: WebGLUniformLocation | null; col: WebGLUniformLocation | null }

function setupGL(canvas: HTMLCanvasElement): GL | null {
  let gl: WebGLRenderingContext | null = null;
  try {
    gl = canvas.getContext("webgl", { premultipliedAlpha: true, antialias: false, alpha: true });
  } catch {
    return null;
  }
  if (!gl) return null;
  const shader = (type: number, src: string) => {
    const s = gl!.createShader(type)!;
    gl!.shaderSource(s, src);
    gl!.compileShader(s);
    return s;
  };
  const prog = gl.createProgram()!;
  gl.attachShader(prog, shader(gl.VERTEX_SHADER, VS));
  gl.attachShader(prog, shader(gl.FRAGMENT_SHADER, FS));
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) return null;
  gl.useProgram(prog);
  gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
  const lp = gl.getAttribLocation(prog, "p");
  const ld = gl.getAttribLocation(prog, "d");
  gl.enableVertexAttribArray(lp);
  gl.enableVertexAttribArray(ld);
  gl.vertexAttribPointer(lp, 2, gl.FLOAT, false, 20, 0);
  gl.vertexAttribPointer(ld, 3, gl.FLOAT, false, 20, 8);
  gl.enable(gl.BLEND);
  gl.blendFunc(gl.ONE, gl.ONE);
  return { gl, res: gl.getUniformLocation(prog, "res"), col: gl.getUniformLocation(prog, "col") };
}

export interface OrbOptions {
  particles: number;
  reducedMotion: boolean;
  interactive?: boolean;
}

const POINTER_RADIUS = 150; // in the orb's 720-unit drawing space
const RIPPLE_SECONDS = 1.4;

export class OrbRenderer {
  private readonly gl: GL | null;
  private readonly halo: CanvasRenderingContext2D | null;
  private params: Look & { level: number; env: number };
  private state: OrbState = "CONNECTING";
  private time = 0;
  private visible = true;
  private running = false;
  private buf = new Float32Array(0);
  private onsets: number[] = [];
  private levelSource: (() => number) | null = null;
  private agents = 2;
  private tween: gsap.core.Tween | null = null;
  private readonly observer: IntersectionObserver | null;
  private readonly onVisibility = () => this.syncLoop();
  // the pointer, in the orb's drawing space; power fades in near the orb and out when the pointer leaves
  private client: { x: number; y: number } | null = null;
  private pointer = { x: 360, y: 360, power: 0, yaw: 0, pitch: 0 };
  private ripples: number[] = [];
  private listening = false;
  private readonly onPointerMove = (e: PointerEvent) => { this.client = { x: e.clientX, y: e.clientY }; };
  private readonly onPointerOut = () => { this.client = null; };
  private readonly onPointerDown = (e: PointerEvent) => {
    const rect = this.dotsCanvas.getBoundingClientRect();
    const dx = e.clientX - (rect.left + rect.width / 2), dy = e.clientY - (rect.top + rect.height / 2);
    if (Math.hypot(dx, dy) < rect.width * 0.36) this.ripples.push(this.time);
  };

  constructor(private readonly haloCanvas: HTMLCanvasElement, private readonly dotsCanvas: HTMLCanvasElement,
    private options: OrbOptions) {
    this.gl = setupGL(dotsCanvas);
    this.halo = haloCanvas.getContext?.("2d") ?? null;
    this.params = { ...structuredClone(LOOKS.CONNECTING), level: 0, env: 0 };
    this.observer = typeof IntersectionObserver === "function"
      ? new IntersectionObserver((entries) => { this.visible = entries.some((e) => e.isIntersecting); this.syncLoop(); })
      : null;
    this.observer?.observe(dotsCanvas);
    document.addEventListener("visibilitychange", this.onVisibility);
    this.resize();
  }

  /** true when the orb can actually draw (WebGL available); otherwise the page shows its text state only. */
  get drawable(): boolean {
    return this.gl !== null;
  }

  setOptions(options: Partial<OrbOptions>): void {
    this.options = { ...this.options, ...options };
    this.syncLoop();
    this.paintOnce();
  }

  private syncPointer(want: boolean): void {
    if (want === this.listening) return;
    this.listening = want;
    const method = want ? "addEventListener" : "removeEventListener";
    window[method]("pointermove", this.onPointerMove as EventListener, { passive: true } as AddEventListenerOptions);
    window[method]("pointerdown", this.onPointerDown as EventListener, { passive: true } as AddEventListenerOptions);
    window[method]("blur", this.onPointerOut);
    document.documentElement[method]("pointerleave", this.onPointerOut);
    if (!want) this.client = null;
  }

  /** Real audio only: the microphone while listening, the TTS playback while speaking. */
  setLevelSource(source: (() => number) | null): void {
    this.levelSource = source;
  }

  setAgents(count: number): void {
    this.agents = Math.max(2, Math.min(6, Math.round(count) || 2));
  }

  setState(state: OrbState): void {
    if (state === this.state) return;
    this.state = state;
    const look = LOOKS[state];
    this.tween?.kill();
    const target = { ...look, forms: [...look.forms] };
    if (this.options.reducedMotion) {
      this.params = { ...this.params, ...structuredClone(target) };
      this.paintOnce();
      return;
    }
    // one tween carries every parameter from where it is now (interruptible: a new state starts from the live value)
    const from = this.params;
    const forms = [...from.forms];
    this.tween = gsap.to(from, {
      spin: look.spin, wave: look.wave, radius: look.radius, alpha: look.alpha, glow: look.glow, pace: look.pace,
      scan: look.scan, breathe: look.breathe, jitter: look.jitter, duration: 0.7, ease: "power2.out",
      onUpdate: () => {
        const p = this.tween?.progress() ?? 1;
        from.forms = forms.map((w, i) => w + ((look.forms[i] ?? 0) - w) * p);
        from.color = from.color.map((c, i) => c + (look.color[i]! - c) * Math.min(1, p * 1.4)) as RGB;
      },
    });
  }

  resize(): void {
    const rect = this.dotsCanvas.getBoundingClientRect();
    const size = Math.max(1, Math.round(Math.max(rect.width, 1) * Math.min(window.devicePixelRatio || 1, 2)));
    this.dotsCanvas.width = this.dotsCanvas.height = this.haloCanvas.width = this.haloCanvas.height = size;
    this.paintOnce();
  }

  start(): void {
    this.running = true;
    this.syncLoop();
    this.paintOnce();
  }

  destroy(): void {
    this.running = false;
    this.tween?.kill();
    gsap.ticker.remove(this.tick);
    this.syncPointer(false);
    this.observer?.disconnect();
    document.removeEventListener("visibilitychange", this.onVisibility);
    const lose = this.gl?.gl.getExtension("WEBGL_lose_context");
    lose?.loseContext();
  }

  private looping = false;
  private syncLoop(): void {
    const want = this.running && this.visible && !document.hidden && !this.options.reducedMotion && this.gl !== null;
    if (want && !this.looping) {
      gsap.ticker.add(this.tick);
      this.looping = true;
    } else if (!want && this.looping) {
      gsap.ticker.remove(this.tick);
      this.looping = false;
    }
    this.syncPointer(want && this.options.interactive === true);
  }

  /** follow the pointer smoothly: where it is (in drawing units), how close to the orb, and the turn towards it */
  private trackPointer(): void {
    const ptr = this.pointer;
    let target = 0;
    if (this.client) {
      const rect = this.dotsCanvas.getBoundingClientRect();
      if (rect.width > 0) {
        const x = ((this.client.x - rect.left) / rect.width) * 720, y = ((this.client.y - rect.top) / rect.height) * 720;
        const d = Math.hypot(x - 360, y - 360);
        target = Math.max(0, Math.min(1, 1 - (d - 250) / 300));
        ptr.x += (x - ptr.x) * 0.3;
        ptr.y += (y - ptr.y) * 0.3;
      }
    }
    ptr.power += (target - ptr.power) * (target > ptr.power ? 0.12 : 0.05);
    const clamp = (v: number) => Math.max(-1, Math.min(1, v));
    ptr.yaw += (clamp((ptr.x - 360) / 360) * 0.42 * ptr.power - ptr.yaw) * 0.08;
    ptr.pitch += (clamp((ptr.y - 360) / 360) * 0.42 * ptr.power - ptr.pitch) * 0.08;
    this.ripples = this.ripples.filter((r) => this.time - r < RIPPLE_SECONDS);
  }

  private readonly tick = (_time: number, deltaMs: number) => {
    const dt = Math.min(deltaMs / 1000, 0.05);
    this.time += dt * this.params.pace;
    const raw = this.levelSource ? Math.max(0, Math.min(1, this.levelSource())) : 0;
    const prev = this.params.env;
    this.params.env += (raw - this.params.env) * (raw > this.params.env ? 0.55 : 0.18);
    this.params.level = this.params.env;
    // a syllable is a fast rise in the playback envelope; each sends a lit ring up the sphere while speaking
    if (this.state === "SPEAKING" && this.params.env - prev > 0.06 && this.time - (this.onsets.at(-1) ?? -1) > 0.12) {
      this.onsets.push(this.time);
    }
    this.onsets = this.onsets.filter((o) => this.time - o < 1.2);
    if (this.options.interactive) this.trackPointer();
    this.paint();
  };

  private paintOnce(): void {
    if (this.gl) this.paint();
  }

  private paint(): void {
    const size = this.dotsCanvas.width;
    if (!size || !this.gl) return;
    const P = this.params;
    const t = this.options.reducedMotion ? 0 : this.time;
    const env = this.options.reducedMotion ? 0 : P.env;
    const unit = size / 720;
    const rgb = P.color.map((c) => Math.round(c)) as RGB;

    const ptr = this.pointer;
    const live = this.options.reducedMotion ? 0 : 1;
    if (this.halo) {
      const g = this.halo;
      g.setTransform(unit, 0, 0, unit, 0, 0);
      g.clearRect(0, 0, 720, 720);
      // a living glow: brighter than the particles alone, breathing slowly, lifting with the voice and the pointer
      const glow = P.glow * 1.3 * (1 + 0.5 * env + 0.15 * ptr.power * live) * (1 + 0.08 * Math.sin(t * 1.25) * live);
      const core = rgb.map((c) => Math.round(c + (255 - c) * 0.55)).join(",");
      const halo = g.createRadialGradient(360, 360, 6, 360, 360, 330 + 30 * env);
      halo.addColorStop(0, `rgba(${core},${Math.min(1, glow * 1.6)})`);
      halo.addColorStop(0.16, `rgba(${rgb.join(",")},${Math.min(1, glow * 1.2)})`);
      halo.addColorStop(0.42, `rgba(${rgb.join(",")},${glow * 0.6})`);
      halo.addColorStop(0.72, `rgba(${rgb.join(",")},${glow * 0.18})`);
      halo.addColorStop(1, `rgba(${rgb.join(",")},0)`);
      g.fillStyle = halo;
      g.fillRect(0, 0, 720, 720);
    }

    const count = this.options.particles;
    const seed = seeds(count);
    if (this.buf.length < count * 5) this.buf = new Float32Array(count * 5);
    const buf = this.buf;
    const [w0, w1, w2, w3, w4, w5, w6] = P.forms as [number, number, number, number, number, number, number];
    const spin = t * P.spin * TAU, cs = Math.cos(spin), sn = Math.sin(spin);
    const breath = 1 + P.breathe * Math.sin(t * 2.4);
    const R = 190 * P.radius * breath;
    const ring = Math.round(count * 0.65);
    const rings = this.onsets.map((o) => { const age = this.time - o; return { h: -1.15 + age * 2.1, g: (1 - age / 1.2) ** 2 }; });
    const sweep = t * 1.6;
    const clusters = this.agents;
    const fine = Math.max(0.5, Math.pow(1000 / count, 0.55));
    const light = Math.min(1, Math.pow(1000 / (count * fine * fine), 0.35));
    const touch = this.options.interactive && live ? ptr.power : 0;
    const cy = Math.cos(ptr.yaw * live), sy = Math.sin(ptr.yaw * live), cp = Math.cos(ptr.pitch * live), sp = Math.sin(ptr.pitch * live);
    const turned = Math.abs(ptr.yaw) + Math.abs(ptr.pitch) > 0.002 && live > 0;
    const waves = live ? this.ripples.map((r) => { const age = this.time - r; return { r: age * 430, g: (1 - age / RIPPLE_SECONDS) ** 2 }; }) : [];

    for (let i = 0; i < count; i++) {
      const p = seed[i]!;
      let vx = 0, vy = 0, vz = 0, lit = 0;
      const rx = p.x * cs + p.z * sn, rz = p.z * cs - p.x * sn;
      if (w0 > 0.001) { // sphere: a calm surface; listening ripples with the real microphone level
        let r = R + Math.sin(p.y * 11 - t * 3 + p.a * 0.03) * P.wave * (1 + 3 * env) + Math.sin(p.a * 0.17 + t * 2) * 2;
        if (P.jitter > 0.001) r += Math.sin(i * 7.31 + t * 9.7) * Math.sin(t * 3.3 + p.y * 5) * 6 * P.jitter;
        if (P.scan > 0.001) { // executing: a lit meridian sweeping round
          const lon = Math.atan2(rz, rx);
          const d = Math.atan2(Math.sin(lon - sweep), Math.cos(lon - sweep));
          const g = Math.exp(-(d * d) / 0.02) * P.scan;
          r += 5 * g;
          lit += g * 0.9;
        }
        vx += rx * r * w0; vy += p.y * r * w0; vz += rz * r * w0;
      }
      if (w1 > 0.001) { // thinking: a slow knot turning over
        const theta = p.u * TAU * 3 + t * 0.57, phi = p.u * TAU * 8 - t * 0.48, rt = 137 + 41 * Math.cos(phi) + 24 * p.sa;
        const bx = rt * Math.cos(theta), bz = rt * Math.sin(theta), by = 65 * Math.sin(phi) + 17 * p.y;
        vx += bx * w1; vy += (by * 0.77 - bz * 0.52) * w1; vz += (by * 0.52 + bz * 0.77) * w1;
      }
      if (w2 > 0.001) { // searching: three orbits scanning
        const band = i % 3, angle = p.u * TAU * 5 + t * (0.7 + band * 0.2), radius = 196 + 11 * p.sa;
        const sx = Math.cos(angle) * radius, sy = Math.sin(angle) * radius;
        const tilt = BAND_TILT[band]!;
        const sy2 = sy * tilt[0], sz = sy * tilt[1];
        vx += (sx * tilt[2] - sy2 * tilt[3]) * w2; vy += (sx * tilt[3] + sy2 * tilt[2]) * w2; vz += sz * w2;
      }
      if (w3 > 0.001) { // speaking: the real voice inflates a lobed sphere; each syllable sends a ring upward
        const lobe = Math.sin(rx * 2.3 + t * 1.4) * Math.sin(p.y * 2.9 - t * 1.1) * Math.cos(rz * 2.1 + t * 0.8);
        let r = 184 + 16 * env + lobe * (5 + 19 * env) + Math.sin(p.y * 7 - t * 2.2) * 3 * env;
        let rl = 0;
        for (const ringPulse of rings) { const d = (p.y - ringPulse.h) / 0.15; const g = Math.exp(-d * d) * ringPulse.g; r += 7 * g; rl += g; }
        vx += rx * r * w3; vy += p.y * r * w3; vz += rz * r * w3; lit += rl * w3;
      }
      if (w4 > 0.001) { // completed: a ring and a check
        if (i < ring) { const q = (i / ring) * TAU, rr = 173 + 9 * p.sa; vx += Math.cos(q) * rr * w4; vy += Math.sin(q) * rr * w4; vz += 8 * p.sa * w4; }
        else {
          const q = (i - ring) / (count - ring), first = q < 0.38, v = first ? q / 0.38 : (q - 0.38) / 0.62;
          vx += ((first ? -81 + 58 * v : -23 + 116 * v) + 6 * p.sa) * w4; vy += ((first ? 57 * v : 57 - 133 * v) + 6 * p.ca) * w4;
        }
      }
      if (w5 > 0.001) { // planning: the sphere settles into ordered latitude rings that turn in steps
        const yq = Math.round(p.y * 4) / 4, rr = Math.sqrt(Math.max(0, 1 - yq * yq));
        const step = Math.floor(t * 0.8) + (1 - Math.cos(Math.PI * ((t * 0.8) % 1))) / 2;
        const ang = p.a + step * 0.5 * (yq * 4 % 2 ? 1 : -1);
        vx += Math.cos(ang) * rr * 188 * w5; vy += yq * 188 * w5; vz += Math.sin(ang) * rr * 188 * w5;
      }
      if (w6 > 0.001) { // orchestrating: a core and one satellite per agent really working
        const c = i % (clusters + 1);
        if (c === clusters) { vx += rx * 92 * w6; vy += p.y * 92 * w6; vz += rz * 92 * w6; }
        else {
          const ang = (c / clusters) * TAU + t * 0.35, cx = Math.cos(ang) * 178, cz = Math.sin(ang) * 178;
          const cy = Math.sin(ang * 2 + c) * 26;
          vx += (cx + p.x * 34) * w6; vy += (cy * 0.6 + cz * 0.25 + p.y * 34) * w6; vz += (cz * 0.8 + p.z * 34) * w6;
        }
      }
      if (turned) { // the sphere turns a little towards the pointer
        const x1 = vx * cy + vz * sy, z1 = vz * cy - vx * sy;
        const y1 = vy * cp + z1 * sp;
        vz = z1 * cp - vy * sp; vx = x1; vy = y1;
      }
      const depth = (vz + 240) / 480, persp = 850 / (850 - vz);
      let sx = 360 + vx * persp, sy2 = 360 + vy * persp;
      if (touch > 0.01) { // particles part around the pointer and light up near it
        const dx = sx - ptr.x, dy = sy2 - ptr.y, d = Math.hypot(dx, dy) || 1;
        if (d < POINTER_RADIUS) {
          const f = (1 - d / POINTER_RADIUS) ** 2 * touch * (0.45 + 0.55 * depth);
          sx += (dx / d) * f * 48; sy2 += (dy / d) * f * 48; lit += f * 1.1;
        }
      }
      for (const wave of waves) { // a click: a ring of light running out through the orb
        const dx = sx - 360, dy = sy2 - 360, d = Math.hypot(dx, dy) || 1, k = (d - wave.r) / 38;
        const g = Math.exp(-k * k) * wave.g;
        sx += (dx / d) * g * 16; sy2 += (dy / d) * g * 16; lit += g * 0.9;
      }
      const alpha = (0.34 + 0.86 * depth) * P.alpha + 0.3 * env * (w3 + w0 * (this.state === "LISTENING" ? 1 : 0));
      const r = (0.95 + depth * 1.45) * (0.75 + 0.25 * Math.sin(p.a + t * 1.6)) * fine * (1 + 0.5 * Math.min(1, lit));
      const o = i * 5;
      buf[o] = sx * unit;
      buf[o + 1] = sy2 * unit;
      buf[o + 2] = r * 9 * unit;
      buf[o + 3] = Math.min(1, (alpha + 1.2 * lit) * light);
      buf[o + 4] = vz > 75 || lit > 0.25 ? (Math.max(alpha - 0.5, 0) + lit * 0.8) * 0.75 * light : 0;
    }
    const { gl, res, col } = this.gl;
    gl.viewport(0, 0, size, size);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.uniform2f(res, size, size);
    gl.uniform3f(col, rgb[0] / 255, rgb[1] / 255, rgb[2] / 255);
    gl.bufferData(gl.ARRAY_BUFFER, buf.subarray(0, count * 5), gl.DYNAMIC_DRAW);
    gl.drawArrays(gl.POINTS, 0, count);
  }
}
