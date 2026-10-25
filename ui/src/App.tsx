import { lazy, Suspense, useEffect, useRef } from "react";
import { Shell } from "./components/Shell";
import { Loading } from "./components/ui";
import { arrive, useGsap, useReducedMotion } from "./motion/motion";
import { HomePage } from "./pages/Home";
import { useAppState } from "./state/context";
import { useRoute, type Route } from "./state/router";

// Home is the first screen; every other page loads when it is first opened.
const AgentsPage = lazy(() => import("./pages/Agents").then((m) => ({ default: m.AgentsPage })));
const OrchestrationPage = lazy(() => import("./pages/Orchestration").then((m) => ({ default: m.OrchestrationPage })));
const TasksPage = lazy(() => import("./pages/Tasks").then((m) => ({ default: m.TasksPage })));
const SchedulesPage = lazy(() => import("./pages/Schedules").then((m) => ({ default: m.SchedulesPage })));
const ToolsPage = lazy(() => import("./pages/Tools").then((m) => ({ default: m.ToolsPage })));
const MemoryPage = lazy(() => import("./pages/Memory").then((m) => ({ default: m.MemoryPage })));
const ActivityPage = lazy(() => import("./pages/Activity").then((m) => ({ default: m.ActivityPage })));
const ApprovalsPage = lazy(() => import("./pages/Approvals").then((m) => ({ default: m.ApprovalsPage })));
const IntegrationsPage = lazy(() => import("./pages/SystemPages").then((m) => ({ default: m.IntegrationsPage })));
const SystemPage = lazy(() => import("./pages/SystemPages").then((m) => ({ default: m.SystemPage })));
const SettingsPage = lazy(() => import("./pages/SystemPages").then((m) => ({ default: m.SettingsPage })));

function Page({ route }: { route: Route }) {
  switch (route.page) {
    case "home": return <HomePage />;
    case "agents": return <AgentsPage />;
    case "orchestration": return <OrchestrationPage runId={route.param} />;
    case "tasks": return <TasksPage />;
    case "schedules": return <SchedulesPage />;
    case "tools": return <ToolsPage />;
    case "memory": return <MemoryPage />;
    case "activity": return <ActivityPage />;
    case "approvals": return <ApprovalsPage />;
    case "integrations": return <IntegrationsPage />;
    case "system": return <SystemPage />;
    case "settings": return <SettingsPage />;
  }
}

export function App() {
  const route = useRoute();
  const loadError = useAppState((s) => s.loadError);
  const stream = useAppState((s) => s.stream);
  const page = useRef<HTMLDivElement>(null);
  const reduced = useReducedMotion();
  const textSize = useAppState((s) => s.settings?.text_size ?? "default");
  useEffect(() => { document.documentElement.dataset.motion = reduced ? "reduced" : "full"; }, [reduced]);
  useEffect(() => { document.documentElement.dataset.textSize = textSize; }, [textSize]);
  useGsap(page, (reduced) => { if (page.current) arrive(page.current, reduced); }, [route.page]);
  useEffect(() => {
    document.title = route.page === "home" ? "XENIO" : `${route.page[0]!.toUpperCase()}${route.page.slice(1)} · XENIO`;
    document.getElementById("main")?.focus({ preventScroll: true });
  }, [route.page]);

  if (stream === "unauthorized") return <SignIn reason="This page's session key is not valid any more (XENIO was restarted)." />;
  return (
    <Shell page={route.page}>
      {loadError && <p className="banner banner--danger" role="alert">XENIO could not be reached: {loadError}. It retries on its own.</p>}
      <div ref={page} key={route.page} className="page">
        <Suspense fallback={<Loading what="the page" />}><Page route={route} /></Suspense>
      </div>
    </Shell>
  );
}

export function SignIn({ reason }: { reason?: string }) {
  return (
    <main className="signin">
      <h1>Open XENIO from its own link</h1>
      {reason && <p>{reason}</p>}
      <p>For your safety this interface only works with the private address XENIO prints when it starts. Run this in the
        XENIO folder, then open the address it shows (it opens by itself unless you used --no-browser):</p>
      <pre><code>.venv/bin/python -m app.main --ui</code></pre>
    </main>
  );
}
