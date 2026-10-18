import { useMemo, useRef } from "react";
import type { OrbState } from "../api/types";
import { describe, deriveOrbState, nextState, type StateCopy } from "../orb/machine";
import { useApp, useAppState } from "../state/context";

export interface OrbView extends StateCopy {
  state: OrbState;
  level: (() => number) | null;
  agents: number;
}

/** The orb's state from live inputs, passed through the transition rules; plus the real audio level when there is one. */
export function useOrbState(): OrbView {
  const { app } = useApp();
  const stream = useAppState((s) => s.stream);
  const assistant = useAppState((s) => s.assistant);
  const voice = useAppState((s) => s.voice);
  const previous = useRef<OrbState>("CONNECTING");
  const target = deriveOrbState(stream, assistant, voice);
  const state = nextState(previous.current, target);
  previous.current = state;
  const engine = assistant?.voice === "listening" || assistant?.voice === "speaking";
  const level = voice.listening ? app.micLevel : voice.speaking ? app.speaker.level : engine ? app.engineLevel : null;
  const copy = useMemo(() => describe(state, assistant), [state, assistant]);
  return { ...copy, state, level, agents: assistant?.background.agents ?? 0 };
}
