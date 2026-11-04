import { createContext, useContext, type ReactNode } from "react";
import type { AppState } from "./store";
import { Store, useStore } from "./store";
import type { AppController } from "./controller";

interface Ctx {
  store: Store;
  app: AppController;
}

const AppContext = createContext<Ctx | null>(null);

export function AppProvider({ store, app, children }: Ctx & { children: ReactNode }) {
  return <AppContext.Provider value={{ store, app }}>{children}</AppContext.Provider>;
}

/** For parts that also appear outside the app (a lesson's text in a test): null when there is no app around them. */
export function useOptionalApp(): Ctx | null {
  return useContext(AppContext);
}

export function useApp(): Ctx {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error("useApp outside <AppProvider>");
  return ctx;
}

export function useAppState<T>(select: (s: AppState) => T): T {
  return useStore(useApp().store, select);
}
