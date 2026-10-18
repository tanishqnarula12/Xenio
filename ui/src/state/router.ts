import { useEffect, useState } from "react";

export const PAGES = [
  { id: "home", label: "Home" },
  { id: "agents", label: "Agents" },
  { id: "orchestration", label: "Orchestration" },
  { id: "tasks", label: "Tasks & projects" },
  { id: "schedules", label: "Schedules" },
  { id: "tools", label: "Skills & tools" },
  { id: "memory", label: "Memory" },
  { id: "activity", label: "Activity" },
  { id: "approvals", label: "Approvals" },
  { id: "integrations", label: "Integrations" },
  { id: "system", label: "System" },
  { id: "settings", label: "Settings" },
] as const;

export type PageId = (typeof PAGES)[number]["id"];

export interface Route {
  page: PageId;
  param: string | null;
}

export function parseRoute(hash: string): Route {
  const [page = "home", param = null] = hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent);
  const known = PAGES.find((p) => p.id === page);
  return { page: known ? known.id : "home", param: known ? param : null };
}

export function href(page: PageId, param?: string): string {
  return `#/${page}${param ? `/${encodeURIComponent(param)}` : ""}`;
}

export function useRoute(): Route {
  const [route, setRoute] = useState(() => parseRoute(window.location.hash));
  useEffect(() => {
    const onHash = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  return route;
}
