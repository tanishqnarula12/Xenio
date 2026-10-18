import { useDeferredValue, useState } from "react";
import { Empty, Loaded, PageHeader, Section } from "../components/ui";
import { useApp } from "../state/context";
import { useResource } from "../state/useResource";

export function MemoryPage() {
  const { app } = useApp();
  const [query, setQuery] = useState("");
  const deferred = useDeferredValue(query);
  const memory = useResource((s) => app.api.memory(deferred, s), [], [deferred]);
  return (
    <>
      <PageHeader title="Memory" intro={<>What XENIO remembers across conversations, as Hermes keeps it. To change it, tell XENIO: "remember that I prefer short answers", "forget my old address". Secrets are never stored.</>} />
      <div className="toolbar">
        <input type="search" className="search" placeholder="Search memory" aria-label="Search memory" value={query}
          onChange={(e) => setQuery(e.target.value)} />
      </div>
      <Loaded resource={memory} what="memory">
        {(data) => {
          if (!data.available) return <Empty>Memory is not available.</Empty>;
          const about = data.entries.filter((e) => e.store === "user");
          const notes = data.entries.filter((e) => e.store === "memory");
          if (!data.entries.length) return <Empty>{query ? "Nothing in memory matches that." : "XENIO has not remembered anything yet."}</Empty>;
          return (
            <>
              <Section title="About you" id="mem-user">
                {about.length ? <ul className="memory">{about.map((e, i) => <li key={i}>{e.text}</li>)}</ul> : <Empty>Nothing about you yet.</Empty>}
              </Section>
              <Section title="XENIO's notes" id="mem-notes">
                {notes.length ? <ul className="memory">{notes.map((e, i) => <li key={i}>{e.text}</li>)}</ul> : <Empty>No notes yet.</Empty>}
              </Section>
            </>
          );
        }}
      </Loaded>
    </>
  );
}
