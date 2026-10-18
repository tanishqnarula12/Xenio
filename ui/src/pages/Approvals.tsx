import { ApprovalCard } from "../components/ApprovalCard";
import { Empty, Loaded, PageHeader, Section, StatusOf, Time } from "../components/ui";
import { useApp, useAppState } from "../state/context";
import { useResource } from "../state/useResource";

export function ApprovalsPage() {
  const { app } = useApp();
  const pending = useAppState((s) => s.approvals);
  const history = useResource((s) => app.api.approvals(s), ["approvals"]);
  return (
    <>
      <PageHeader title="Approvals" intro="Actions XENIO will only take with your yes: sending, deleting, installing, pushing, paying. XENIO decides what needs asking and checks every answer; nothing is ever approved by silence." />
      <Section title="Waiting for you" id="approvals-pending">
        {pending.length ? <div className="approval-list">{pending.map((a) => <ApprovalCard key={a.id} approval={a} />)}</div>
          : <Empty>Nothing is waiting for your decision.</Empty>}
      </Section>
      <Section title="Recent decisions" id="approvals-history">
        <Loaded resource={history} what="decisions">
          {(data) => data.history.length ? (
            <table className="table">
              <caption className="sr-only">Recent decisions</caption>
              <thead><tr><th scope="col">Action</th><th scope="col">Outcome</th><th scope="col">Asked</th><th scope="col">Decided</th></tr></thead>
              <tbody>
                {data.history.map((a) => (
                  <tr key={a.id}>
                    <td>{a.description}</td>
                    <td><StatusOf value={a.status} /></td>
                    <td><Time iso={a.requested_at} /></td>
                    <td><Time iso={a.decided_at} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <Empty>No decisions since XENIO started.</Empty>}
        </Loaded>
      </Section>
    </>
  );
}
