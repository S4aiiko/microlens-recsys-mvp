import { useMemo, useState } from "react";
import type { AdminItem, OnlineStatus, Role } from "../../api/generated";
import { hasCapability } from "../../auth/capabilities";
import { useSession } from "../../auth/session";
import type { AdminApi } from "./admin-api";
import { adminApi } from "./admin-api";
import { AdminError, AdminNotice, JsonValue, StatusBadge } from "./AdminPrimitives";
import { formatShanghai, utcToShanghaiLocal } from "./admin-time";
import type { OperationDraft } from "./operation-submission";
import { useOperationsController } from "./useOperationsController";
import "./admin.css";

const OPERATION_KINDS = ["promote", "offline", "restore"] as const;
const FEEDS = ["personalized", "popular", "explore"] as const;

function initialDraft(): OperationDraft {
  return {
    endsLocal: "",
    kind: "promote",
    maxAttempts: 3,
    priority: 0,
    reason: "",
    scheduled: false,
    scopeType: "all",
    scopeValue: "",
    startsLocal: utcToShanghaiLocal(new Date()),
    targetPosition: null,
  };
}

function roleFromSession(state: ReturnType<typeof useSession>["state"]): Role {
  return state.status === "authenticated" ? state.user.role : "user";
}

function ItemSearch({
  canWrite,
  controller,
  onSelectionError,
  selected,
  setSelected,
}: {
  canWrite: boolean;
  controller: ReturnType<typeof useOperationsController>;
  onSelectionError(message: string | null): void;
  selected: ReadonlyMap<string, AdminItem>;
  setSelected(value: Map<string, AdminItem>): void;
}) {
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState<OnlineStatus | "all">("all");

  function toggle(item: AdminItem) {
    const next = new Map(selected);
    if (next.has(item.item_id)) {
      next.delete(item.item_id);
      onSelectionError(null);
    } else if (next.size >= 100) {
      onSelectionError("A batch can contain at most 100 items.");
      return;
    } else {
      next.set(item.item_id, item);
      onSelectionError(null);
    }
    setSelected(next);
  }

  return (
    <section aria-labelledby="admin-items-heading" className="admin-section">
      <div className="admin-section__heading">
        <div>
          <p className="eyebrow">Authoritative content state</p>
          <h2 id="admin-items-heading">Items</h2>
        </div>
        {canWrite ? <span>{selected.size} / 100 selected</span> : <span>Read-only</span>}
      </div>
      <form
        className="admin-filterbar admin-filterbar--compact"
        onSubmit={(event) => {
          event.preventDefault();
          void controller.search(query, status === "all" ? null : status);
        }}
      >
        <label>
          ID or title
          <input onChange={(event) => setQuery(event.target.value)} value={query} />
        </label>
        <label>
          Status
          <select
            onChange={(event) => setStatus(event.target.value as OnlineStatus | "all")}
            value={status}
          >
            <option value="all">All</option>
            <option value="online">Online</option>
            <option value="offline">Offline</option>
          </select>
        </label>
        <button className="button button--secondary" disabled={controller.itemsLoading} type="submit">
          {controller.itemsLoading ? "Searching" : "Search"}
        </button>
      </form>
      {controller.itemsError ? <AdminError error={controller.itemsError} /> : null}
      <div className="admin-table-wrap">
        <table>
          <thead>
            <tr>
              {canWrite ? <th scope="col">Select</th> : null}
              <th scope="col">Item</th>
              <th scope="col">Title</th>
              <th scope="col">Heat</th>
              <th scope="col">Status</th>
              <th scope="col">Updated</th>
              <th scope="col">Version</th>
            </tr>
          </thead>
          <tbody>
            {controller.items.map((item) => (
              <tr key={item.item_id}>
                {canWrite ? (
                  <td>
                    <input
                      aria-label={`Select ${item.title}`}
                      checked={selected.has(item.item_id)}
                      onChange={() => toggle(item)}
                      type="checkbox"
                    />
                  </td>
                ) : null}
                <td><code>{item.item_id}</code></td>
                <td>{item.title}</td>
                <td>{item.heat}</td>
                <td><StatusBadge value={item.online_status} /></td>
                <td>{formatShanghai(item.updated_at)}</td>
                <td>{item.state_version}</td>
              </tr>
            ))}
            {!controller.items.length && !controller.itemsLoading ? (
              <tr><td colSpan={canWrite ? 7 : 6}>No items match this search.</td></tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function OperationForm({
  controller,
  selectedItems,
}: {
  controller: ReturnType<typeof useOperationsController>;
  selectedItems: readonly AdminItem[];
}) {
  const [draft, setDraft] = useState<OperationDraft>(() => initialDraft());

  function selectKind(kind: OperationDraft["kind"]) {
    setDraft((current) =>
      kind === "promote"
        ? { ...current, kind }
        : {
            ...current,
            endsLocal: "",
            kind,
            priority: 0,
            scopeType: "all",
            scopeValue: "",
            targetPosition: null,
          },
    );
  }

  const isRetry = controller.mutationError?.kind === "network";
  return (
    <section aria-labelledby="admin-operation-heading" className="admin-section">
      <div className="admin-section__heading">
        <div>
          <p className="eyebrow">All-or-nothing transaction</p>
          <h2 id="admin-operation-heading">Create operation</h2>
        </div>
        <span>{selectedItems.length} targets</span>
      </div>
      <form
        className="admin-operation-form"
        onSubmit={(event) => {
          event.preventDefault();
          void controller.submit(draft, selectedItems);
        }}
      >
        <div className="admin-selected-targets admin-span-two">
          <strong>Selected item snapshots</strong>
          {selectedItems.length ? (
            <ul>
              {selectedItems.map((item) => (
                <li key={item.item_id}>
                  <code>{item.item_id}</code>
                  <span>{item.title} / state v{item.state_version}</span>
                </li>
              ))}
            </ul>
          ) : <span>No targets selected.</span>}
        </div>
        <fieldset className="admin-segmented">
          <legend>Operation</legend>
          {OPERATION_KINDS.map((kind) => (
            <label key={kind}>
              <input
                checked={draft.kind === kind}
                name="operation-kind"
                onChange={() => selectKind(kind)}
                type="radio"
              />
              <span>{kind}</span>
            </label>
          ))}
        </fieldset>
        <label className="admin-span-two">
          Reason
          <textarea
            maxLength={500}
            onChange={(event) => setDraft((current) => ({ ...current, reason: event.target.value }))}
            required
            rows={3}
            value={draft.reason}
          />
        </label>
        <label>
          {draft.scheduled ? "Due at" : "Starts at"} (Asia/Shanghai)
          <input
            onChange={(event) => setDraft((current) => ({ ...current, startsLocal: event.target.value }))}
            required
            type="datetime-local"
            value={draft.startsLocal}
          />
        </label>
        <label className="admin-toggle">
          <input
            checked={draft.scheduled}
            onChange={(event) => setDraft((current) => ({ ...current, scheduled: event.target.checked }))}
            type="checkbox"
          />
          <span>Run as scheduled job</span>
        </label>
        {draft.kind === "promote" ? (
          <>
            <label>
              Scope
              <select
                onChange={(event) =>
                  setDraft((current) => ({
                    ...current,
                    scopeType: event.target.value as OperationDraft["scopeType"],
                    scopeValue: "",
                  }))
                }
                value={draft.scopeType}
              >
                <option value="all">All users</option>
                <option value="user">Specific user</option>
                <option value="feed">Specific feed</option>
              </select>
            </label>
            {draft.scopeType === "user" ? (
              <label>
                User ID
                <input
                  onChange={(event) => setDraft((current) => ({ ...current, scopeValue: event.target.value }))}
                  required
                  value={draft.scopeValue}
                />
              </label>
            ) : null}
            {draft.scopeType === "feed" ? (
              <label>
                Feed
                <select
                  onChange={(event) => setDraft((current) => ({ ...current, scopeValue: event.target.value }))}
                  required
                  value={draft.scopeValue}
                >
                  <option value="">Select a feed</option>
                  {FEEDS.map((feed) => <option key={feed} value={feed}>{feed}</option>)}
                </select>
              </label>
            ) : null}
            <label>
              Priority
              <input
                min={0}
                onChange={(event) => setDraft((current) => ({ ...current, priority: Number(event.target.value) }))}
                step={1}
                type="number"
                value={draft.priority}
              />
            </label>
            <label>
              Target position (optional)
              <input
                min={0}
                onChange={(event) =>
                  setDraft((current) => ({
                    ...current,
                    targetPosition: event.target.value === "" ? null : Number(event.target.value),
                  }))
                }
                step={1}
                type="number"
                value={draft.targetPosition ?? ""}
              />
            </label>
            <label>
              Ends at (optional, Asia/Shanghai)
              <input
                onChange={(event) => setDraft((current) => ({ ...current, endsLocal: event.target.value }))}
                type="datetime-local"
                value={draft.endsLocal}
              />
            </label>
          </>
        ) : null}
        {draft.scheduled ? (
          <label>
            Max attempts
            <input
              max={100}
              min={1}
              onChange={(event) => setDraft((current) => ({ ...current, maxAttempts: Number(event.target.value) }))}
              step={1}
              type="number"
              value={draft.maxAttempts}
            />
          </label>
        ) : null}
        <div className="admin-form-actions admin-span-two">
          <button
            className="button button--primary"
            disabled={controller.mutating || selectedItems.length === 0}
            type="submit"
          >
            {controller.mutating ? "Submitting" : isRetry ? "Retry exact request" : "Submit operation"}
          </button>
          <span>Selected state versions are checked by the server before any target changes.</span>
        </div>
      </form>
      {controller.mutationError ? <AdminError error={controller.mutationError} /> : null}
      {controller.validationError ? <p className="admin-field-error" role="alert">{controller.validationError}</p> : null}
      {controller.receipt ? (
        <AdminNotice>
          <strong>{"job" in controller.receipt ? "Scheduled job accepted" : "Batch accepted"}</strong>
          {"job" in controller.receipt ? (
            <span>Operation <code>{controller.receipt.job.job_id}</code> is {controller.receipt.job.state}.</span>
          ) : (
            <span>Batch <code>{controller.receipt.batch_id}</code> is {controller.receipt.status}. No partial-success receipt is used.</span>
          )}
        </AdminNotice>
      ) : null}
    </section>
  );
}

function ScheduledJobPanel({ controller }: { controller: ReturnType<typeof useOperationsController> }) {
  const [operationId, setOperationId] = useState("");
  return (
    <section aria-labelledby="admin-job-heading" className="admin-section">
      <div className="admin-section__heading"><div><p className="eyebrow">Durable worker state</p><h2 id="admin-job-heading">Scheduled job</h2></div></div>
      <form className="admin-inline-form" onSubmit={(event) => { event.preventDefault(); void controller.lookupJob(operationId); }}>
        <label>Operation ID<input onChange={(event) => setOperationId(event.target.value)} required value={operationId} /></label>
        <button className="button button--secondary" type="submit">Get job</button>
        <button className="button button--ghost" disabled={!operationId.trim()} onClick={() => void controller.cancelJob(operationId)} type="button">Cancel job</button>
      </form>
      {controller.jobError ? <AdminError error={controller.jobError} /> : null}
      {controller.job ? (
        <dl className="admin-definition-list">
          <div><dt>Job</dt><dd><code>{controller.job.job_id}</code></dd></div>
          <div><dt>State</dt><dd><StatusBadge value={controller.job.state} /></dd></div>
          <div><dt>Due</dt><dd>{formatShanghai(controller.job.due_at)}</dd></div>
          <div><dt>Attempts</dt><dd>{controller.job.attempt_count} / {controller.job.max_attempts}</dd></div>
          <div><dt>Last error</dt><dd>{controller.job.last_error ?? "None"}</dd></div>
          <div><dt>Result</dt><dd><JsonValue value={controller.job.result} /></dd></div>
        </dl>
      ) : null}
    </section>
  );
}

function AuditLog({ controller }: { controller: ReturnType<typeof useOperationsController> }) {
  return (
    <section aria-labelledby="admin-audit-heading" className="admin-section">
      <div className="admin-section__heading">
        <div><p className="eyebrow">Server audit trail</p><h2 id="admin-audit-heading">Operations audit</h2></div>
        <button className="button button--ghost" onClick={() => void controller.loadAudit()} type="button">Refresh</button>
      </div>
      {controller.auditError ? <AdminError error={controller.auditError} /> : null}
      <div className="admin-table-wrap">
        <table>
          <thead><tr><th>Effective</th><th>Operation</th><th>Type</th><th>Operator</th><th>Target</th><th>Result</th><th>Reason</th><th>Before</th><th>After</th></tr></thead>
          <tbody>
            {controller.audit.map((entry) => (
              <tr key={entry.operation_id}>
                <td>{formatShanghai(entry.effective_at)}</td>
                <td><code>{entry.operation_id}</code><span className="admin-cell-subtitle">Batch {entry.batch_id}</span></td>
                <td>{entry.operation_type}</td>
                <td><code>{entry.operator_id}</code><span className="admin-cell-subtitle">{entry.operator_role}</span></td>
                <td><code>{entry.target}</code></td>
                <td><StatusBadge value={entry.result} />{entry.error ? <span className="admin-cell-subtitle">{entry.error}</span> : null}</td>
                <td>{entry.reason}</td>
                <td><JsonValue value={entry.before_value} /></td>
                <td><JsonValue value={entry.after_value} /></td>
              </tr>
            ))}
            {!controller.audit.length ? <tr><td colSpan={9}>No audit operations returned.</td></tr> : null}
          </tbody>
        </table>
      </div>
    </section>
  );
}

export function OperationsExperience({ api = adminApi }: { api?: AdminApi }) {
  const session = useSession();
  const role = roleFromSession(session.state);
  const canWrite = hasCapability(role, "operationsWrite");
  const controller = useOperationsController(api);
  const [selected, setSelected] = useState<Map<string, AdminItem>>(() => new Map());
  const [selectionError, setSelectionError] = useState<string | null>(null);
  const selectedItems = useMemo(
    () => [...selected.values()].sort((left, right) => left.item_id.localeCompare(right.item_id)),
    [selected],
  );

  return (
    <section className="admin-shell" aria-labelledby="admin-operations-title">
      <div className="admin-page-heading">
        <div><p className="eyebrow">Content control plane</p><h1 id="admin-operations-title">Operations</h1><p>Search current item state, submit server-authorized changes and inspect the audit trail.</p></div>
        <span className="admin-role-note">Role: <strong>{role}</strong></span>
      </div>
      {!canWrite ? (
        <AdminNotice><strong>Read-only operations view</strong><span>Write controls are unavailable for this role. The server remains authoritative for every request.</span></AdminNotice>
      ) : null}
      {selectionError ? <p className="admin-field-error" role="alert">{selectionError}</p> : null}
      <ItemSearch canWrite={canWrite} controller={controller} onSelectionError={setSelectionError} selected={selected} setSelected={setSelected} />
      {canWrite ? <OperationForm controller={controller} selectedItems={selectedItems} /> : null}
      {canWrite ? <ScheduledJobPanel controller={controller} /> : null}
      <AuditLog controller={controller} />
    </section>
  );
}
