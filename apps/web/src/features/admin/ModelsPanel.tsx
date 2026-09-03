import type { AdminApi } from "./admin-api";
import { adminApi } from "./admin-api";
import { AdminError, JsonValue, StatusBadge } from "./AdminPrimitives";
import { formatShanghai } from "./admin-time";
import { useModelsController } from "./useDiagnosticsController";

export function ModelsPanel({ api = adminApi }: { api?: AdminApi }) {
  const { refresh, state } = useModelsController(api);
  return (
    <section aria-labelledby="models-heading" className="admin-section">
      <div className="admin-section__heading"><div><p className="eyebrow">Read-only registry</p><h2 id="models-heading">Models and training jobs</h2></div><button className="button button--ghost" onClick={() => void refresh()} type="button">Refresh</button></div>
      {state.error ? <AdminError error={state.error} /> : null}
      {state.loading ? <p className="admin-refreshing" role="status">Loading model registry</p> : null}
      <div className="admin-table-wrap"><table><caption>Model versions</caption><thead><tr><th>Version</th><th>Status</th><th>Data</th><th>Purpose</th><th>Comparable</th><th>Eligible</th><th>Metrics</th><th>Trained</th></tr></thead><tbody>{state.models.map((model) => <tr key={model.model_version}><td><code>{model.model_version}</code></td><td><StatusBadge value={model.status} />{model.status === "FAILED" ? <span className="admin-cell-subtitle">Never active</span> : null}</td><td><code>{model.data_version}</code></td><td>{model.purpose}</td><td>{model.evaluation_comparability}</td><td>{model.activation_eligible ? "Eligible" : "Not eligible"}</td><td><JsonValue value={model.metrics} /></td><td>{formatShanghai(model.trained_at)}</td></tr>)}{!state.models.length && !state.loading ? <tr><td colSpan={8}>No model versions registered.</td></tr> : null}</tbody></table></div>
      {state.comparison ? <div className={`admin-alert ${state.comparison.comparable ? "" : "admin-alert--warning"}`}><strong>{state.comparison.comparable ? "Latest versions are comparable" : "Latest versions are not comparable"}</strong><span>{state.comparison.reason ?? `${state.comparison.versions.length} versions use a compatible evaluation window.`}</span></div> : null}
      {state.comparisonError ? <AdminError error={state.comparisonError} title="Model comparison unavailable" /> : null}
      <div className="admin-table-wrap"><table><caption>Training jobs</caption><thead><tr><th>Job</th><th>Status</th><th>Data</th><th>Purpose</th><th>Comparable</th><th>Eligible</th><th>Failure</th></tr></thead><tbody>{state.jobs.map((job) => <tr key={job.job_id}><td><code>{job.job_id}</code></td><td><StatusBadge value={job.status} /></td><td><code>{job.data_version}</code></td><td>{job.purpose}</td><td>{job.evaluation_comparability}</td><td>{job.activation_eligible ? "Eligible" : "Not eligible"}</td><td>{job.failure_reason ?? "-"}</td></tr>)}{!state.jobs.length && !state.loading ? <tr><td colSpan={7}>No training jobs registered.</td></tr> : null}</tbody></table></div>
    </section>
  );
}
