import { Empty, Loaded, PageHeader, Status, StatusOf, Time } from "../components/ui";
import { useApp } from "../state/context";
import { useResource } from "../state/useResource";

const RESULT: Record<string, string> = { message: "Reported something", silent: "Nothing to report", failed: "Failed" };

export function SchedulesPage() {
  const { app } = useApp();
  const schedules = useResource((s) => app.api.schedules(s), ["schedules", "activity"]);
  return (
    <>
      <PageHeader title="Schedules" intro={<>XENIO's scheduled checks run on Hermes' own scheduler, read-only and guarded. To add or remove one, ask XENIO: "check my inbox every morning at 8", "remove the inbox check".</>} />
      <Loaded resource={schedules} what="scheduled checks">
        {(data) => {
          if (!data.jobs.length) return <Empty>{data.note ? "No scheduled checks yet." : "No scheduled checks."} Ask XENIO to set one up.</Empty>;
          return (
            <>
              <p className="meta">
                {data.ticking ? "Checks run while XENIO is open (or a Hermes gateway runs them)." : "Nothing is running the checks right now: start XENIO to run them."}
                {data.timezone ? ` Times are in ${data.timezone}.` : ""}
              </p>
              <ul className="schedules">
                {data.jobs.map((j) => (
                  <li key={j.id} className="schedule">
                    <div className="schedule__head">
                      <h2 className="schedule__name">{j.name}</h2>
                      {j.enabled ? <Status tone="ok">On</Status> : <StatusOf value="disabled" />}
                      {j.last_status === "error" && <Status tone="danger">Last run failed</Status>}
                    </div>
                    <p>{j.task}</p>
                    <dl className="facts">
                      <dt>When</dt><dd>{j.schedule ?? "unknown"}</dd>
                      <dt>Next run</dt><dd><Time iso={j.next_run} /></dd>
                      <dt>Last run</dt><dd><Time iso={j.last_run} /></dd>
                    </dl>
                    {j.last_error && <p className="error-text">{j.last_error}</p>}
                    {!!j.runs.length && (
                      <details>
                        <summary>Recent runs</summary>
                        <ol className="timeline">
                          {j.runs.map((r) => (
                            <li key={r.at} className="timeline__item">
                              <p className="timeline__text">{RESULT[r.result] ?? r.result}{r.message ? `: ${r.message}` : ""}</p>
                              <span className="meta">{r.at}</span>
                            </li>
                          ))}
                        </ol>
                      </details>
                    )}
                  </li>
                ))}
              </ul>
            </>
          );
        }}
      </Loaded>
    </>
  );
}
