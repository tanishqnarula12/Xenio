// XENIO's small motion language (GSAP). Motion answers something that happened: a panel opening from the orb, a page
// arriving, an approval appearing, a status changing. Nothing loops for decoration. Every timeline lives in a GSAP
// context that is reverted when its component goes away, and reduced motion swaps travel for a short fade.

import { gsap } from "gsap";
import { useEffect, useLayoutEffect, useState, type DependencyList, type RefObject } from "react";
import { useAppState } from "../state/context";

export const MOTION = {
  fast: 0.16,
  base: 0.3,
  slow: 0.5,
  ease: "power3.out",
  easeIn: "power2.in",
  rise: 10, // px a page or a block travels as it arrives
} as const;

export function usePrefersReducedMotion(): boolean {
  const query = "(prefers-reduced-motion: reduce)";
  const [reduced, setReduced] = useState(() => typeof matchMedia === "function" && matchMedia(query).matches);
  useEffect(() => {
    if (typeof matchMedia !== "function") return;
    const mq = matchMedia(query);
    const on = () => setReduced(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return reduced;
}

/** The user's choice in Settings wins; "system" follows the operating system's reduce-motion setting. */
export function useReducedMotion(): boolean {
  const system = usePrefersReducedMotion();
  const choice = useAppState((s) => s.settings?.motion ?? "system");
  return choice === "reduced" || (choice === "system" && system);
}

/** Run GSAP code scoped to an element; everything it created is reverted on unmount or when deps change. */
export function useGsap(scope: RefObject<HTMLElement | null>, setup: (reduced: boolean) => void, deps: DependencyList): void {
  const reduced = useReducedMotion();
  useLayoutEffect(() => {
    if (!scope.current) return;
    const ctx = gsap.context(() => setup(reduced), scope.current);
    return () => ctx.revert();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, reduced]);
}

/** A block arriving: rise and fade (reduced: fade only). Content is visible without script; this only animates it in. */
export function arrive(targets: gsap.TweenTarget, reduced: boolean, stagger = 0.05): gsap.core.Tween {
  return reduced
    ? gsap.from(targets, { opacity: 0, duration: MOTION.fast, ease: "none" })
    : gsap.from(targets, { opacity: 0, y: MOTION.rise, duration: MOTION.base, ease: MOTION.ease, stagger, clearProps: "all" });
}
