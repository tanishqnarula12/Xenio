import { useCallback, useEffect, useRef, useState } from "react";
import type { Topic } from "./store";
import { useAppState } from "./context";

export interface Resource<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  /** true while a refresh runs over data already shown */
  refreshing: boolean;
  reload: () => void;
}

/** Load data from XENIO; re-read when one of the topics changes (an event said so). Aborts on unmount. */
export function useResource<T>(load: (signal: AbortSignal) => Promise<T>, topics: Topic[] = [], deps: unknown[] = []): Resource<T> {
  const revision = useAppState((s) => topics.map((t) => s.revision[t]).join(","));
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const hasData = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    load(controller.signal)
      .then((value) => {
        if (controller.signal.aborted) return;
        hasData.current = true;
        setData(value);
        setError(null);
      })
      .catch((e: unknown) => {
        if (controller.signal.aborted || (e instanceof DOMException && e.name === "AbortError")) return;
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revision, tick, ...deps]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading: loading && !hasData.current, refreshing: loading && hasData.current, reload };
}
