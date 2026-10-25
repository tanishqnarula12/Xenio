// jsdom has no layout, media queries, observers or canvas: give components what the browser would.
import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => cleanup());

if (!window.matchMedia) {
  window.matchMedia = (query: string) => ({
    matches: false, media: query, onchange: null, addEventListener: () => {}, removeEventListener: () => {},
    addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false,
  }) as MediaQueryList;
}
class Observer { observe() {} unobserve() {} disconnect() {} takeRecords() { return []; } }
(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver ??= Observer;
(globalThis as unknown as { IntersectionObserver: unknown }).IntersectionObserver ??= Observer;
// no WebGL / 2D canvas in jsdom: the orb falls back to its still image, as on a machine without WebGL
HTMLCanvasElement.prototype.getContext = vi.fn(() => null) as unknown as HTMLCanvasElement["getContext"];
Element.prototype.scrollIntoView ??= function scrollIntoView() {};
// jsdom has no layout, so offsetParent is always null. GSAP then takes every element for a hidden one and briefly
// re-parents it to measure its transform, which drops focus mid-typing (a flaky test). In a browser a displayed element
// has an offset parent, so this never happens there.
Object.defineProperty(HTMLElement.prototype, "offsetParent", {
  configurable: true,
  get(this: HTMLElement) { return this.isConnected ? this.parentElement : null; },
});
