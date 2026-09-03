import { useState } from "react";
import type { PersistedEvent, RecommendationRequestDebugResponse, UserDebugResponse } from "../../api/generated";
import type { AdminApi } from "./admin-api";
import { adminApi } from "./admin-api";
import { AdminError, JsonValue } from "./AdminPrimitives";
import { formatShanghai } from "./admin-time";
import { useDiagnosticsController } from "./useDiagnosticsController";

function ProfileView({ data }: { data: UserDebugResponse }) {
  const summaries = [
    ["Positive", data.profile.positive_summary],
    ["Negative", data.profile.negative_summary],
    ["Dwell", data.profile.dwell_summary],
    ["Revisit", data.profile.revisit_summary],
    ["Share", data.profile.share_summary],
    ["Title preferences", data.profile.title_preferences],
  ] as const;
  return (
    <div className="admin-diagnostic-result">
      <dl className="admin-definition-list">
        <div><dt>User ID</dt><dd><code>{data.user_id}</code></dd></div>
        <div><dt>Profile version</dt><dd>{data.profile.profile_version}</dd></div>
        <div><dt>Updated</dt><dd>{formatShanghai(data.profile.updated_at)}</dd></div>
      </dl>
      <div className="admin-summary-grid">
        {summaries.map(([label, value]) => <div key={label}><h4>{label}</h4><JsonValue value={value} /></div>)}
      </div>
      <h4>Recent interactions</h4>
      {data.profile.recent_interactions.length ? <ol className="admin-event-list">{data.profile.recent_interactions.map((interaction, index) => <li key={index}><JsonValue value={interaction} /></li>)}</ol> : <p className="admin-muted">No recent interactions.</p>}
      <h4>Recent request IDs</h4>
      {data.recent_request_ids.length ? <ul className="admin-id-list">{data.recent_request_ids.map((id) => <li key={id}><code>{id}</code></li>)}</ul> : <p className="admin-muted">No recent requests.</p>}
    </div>
  );
}
function EventRow({ event }: { event: PersistedEvent }) {
  return <tr><td>{formatShanghai(event.server_timestamp)}</td><td>{event.event_type}</td><td><code>{event.item_id}</code></td><td>{event.position}</td><td>{event.duration_ms ?? "-"}</td><td><JsonValue value={event.payload ?? null} /></td></tr>;
}

function RequestView({ data }: { data: RecommendationRequestDebugResponse }) {
  return (
    <div className="admin-diagnostic-result">
      <dl className="admin-definition-list"><div><dt>Request ID</dt><dd><code>{data.request_id}</code></dd></div></dl>
      <div className="admin-grid admin-grid--two">
        <div><h4>Candidate IDs</h4><p className="admin-muted">Per-candidate source and score are unavailable in the current API contract.</p>{data.candidate_item_ids.length ? <ol className="admin-id-list">{data.candidate_item_ids.map((id) => <li key={id}><code>{id}</code><span>Source unavailable · Score unavailable</span></li>)}</ol> : <p>No candidates recorded.</p>}</div>
        <div><h4>Filtered IDs</h4><p className="admin-muted">Per-item filter reasons are unavailable in the current API contract.</p>{data.filtered_item_ids.length ? <ol className="admin-id-list">{data.filtered_item_ids.map((id) => <li key={id}><code>{id}</code><span>Filter reason unavailable</span></li>)}</ol> : <p>No filtered items recorded.</p>}</div>
      </div>
      <h4>Delivered exposures and final ranking</h4>
      <div className="admin-table-wrap"><table><thead><tr><th>Position</th><th>Item</th><th>Source</th><th>Score</th><th>Reason</th><th>Model</th></tr></thead><tbody>{data.ranked_items.map((item) => <tr key={`${item.item_id}:${item.position}`}><td>{item.position}</td><td><code>{item.item_id}</code><span className="admin-cell-subtitle">{item.title}</span></td><td>{item.source}</td><td>{item.score.toFixed(4)}</td><td>{item.reason}</td><td><code>{item.model_version}</code></td></tr>)}{!data.ranked_items.length ? <tr><td colSpan={6}>No delivered exposures.</td></tr> : null}</tbody></table></div>
      <h4>All subsequent events</h4>
      <div className="admin-table-wrap"><table><thead><tr><th>Server time</th><th>Type</th><th>Item</th><th>Position</th><th>Duration ms</th><th>Payload</th></tr></thead><tbody>{data.events.map((event) => <EventRow event={event} key={event.event_id} />)}{!data.events.length ? <tr><td colSpan={6}>No events recorded for this request.</td></tr> : null}</tbody></table></div>
    </div>
  );
}

export function DiagnosticsPanel({ api = adminApi }: { api?: AdminApi }) {
  const controller = useDiagnosticsController(api);
  const [userId, setUserId] = useState("");
  const [requestId, setRequestId] = useState("");
  return (
    <section aria-labelledby="diagnostics-heading" className="admin-section">
      <div className="admin-section__heading"><div><p className="eyebrow">Database trace</p><h2 id="diagnostics-heading">Diagnostics</h2></div></div>
      <div className="admin-grid admin-grid--two">
        <div className="admin-diagnostic">
          <form onSubmit={(event) => { event.preventDefault(); void controller.lookupUser(userId); }}><label>User UUID<input onChange={(event) => setUserId(event.target.value)} placeholder="User ID" required value={userId} /></label><button className="button button--secondary" disabled={controller.user.loading} type="submit">{controller.user.loading ? "Looking up" : "Inspect profile"}</button></form>
          {controller.user.error ? <AdminError error={controller.user.error} /> : null}
          {controller.user.data ? <ProfileView data={controller.user.data} /> : null}
        </div>
        <div className="admin-diagnostic">
          <form onSubmit={(event) => { event.preventDefault(); void controller.lookupRequest(requestId); }}><label>Request UUID<input onChange={(event) => setRequestId(event.target.value)} placeholder="Request ID" required value={requestId} /></label><button className="button button--secondary" disabled={controller.request.loading} type="submit">{controller.request.loading ? "Looking up" : "Inspect trace"}</button></form>
          {controller.request.error ? <AdminError error={controller.request.error} /> : null}
          {controller.request.data ? <RequestView data={controller.request.data} /> : null}
        </div>
      </div>
    </section>
  );
}
