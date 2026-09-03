import { useMemo } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { DashboardBucket, FeedType } from "../../api/generated";
import { useSession } from "../../auth/session";
import { EmptyState, LoadingState } from "../../components/AsyncStates";
import type { AdminApi } from "./admin-api";
import { adminApi } from "./admin-api";
import { ADMIN_TIME_ZONE, formatShanghai } from "./admin-time";
import {
  AdminError,
  AdminNotice,
  Metric,
  formatCount,
  formatDuration,
  formatPercent,
} from "./AdminPrimitives";
import { DiagnosticsPanel } from "./DiagnosticsPanel";
import { ModelsPanel } from "./ModelsPanel";
import { useDashboardController } from "./useDashboardController";
import "./admin.css";

const FEEDS: FeedType[] = ["personalized", "popular", "explore"];
const FEED_COLORS = {
  explore: "#b45309",
  personalized: "#2563eb",
  popular: "#16836f",
} as const;

interface ChartRow {
  bucket: string;
  explore: number;
  personalized: number;
  popular: number;
}

function chartRows(buckets: readonly DashboardBucket[]): ChartRow[] {
  const rows = new Map<string, ChartRow>();
  for (const bucket of buckets) {
    const key = bucket.bucket_start_utc;
    const row = rows.get(key) ?? {
      bucket: formatShanghai(key),
      explore: 0,
      personalized: 0,
      popular: 0,
    };
    row[bucket.feed_type] += bucket.request_count;
    rows.set(key, row);
  }
  return [...rows.values()];
}

function TimeRangeForm({ controller }: { controller: ReturnType<typeof useDashboardController> }) {
  const { filters, setFilters } = controller;
  return (
    <form
      className="admin-filterbar"
      onSubmit={(event) => {
        event.preventDefault();
        controller.applyFilters();
      }}
    >
      <label>
        From
        <input
          onChange={(event) => setFilters((current) => ({ ...current, fromLocal: event.target.value }))}
          required
          type="datetime-local"
          value={filters.fromLocal}
        />
      </label>
      <label>
        To
        <input
          onChange={(event) => setFilters((current) => ({ ...current, toLocal: event.target.value }))}
          required
          type="datetime-local"
          value={filters.toLocal}
        />
      </label>
      <label>
        Feed
        <select
          onChange={(event) =>
            setFilters((current) => ({
              ...current,
              feedType: event.target.value as FeedType | "all",
            }))
          }
          value={filters.feedType}
        >
          <option value="all">All feeds</option>
          {FEEDS.map((feed) => (
            <option key={feed} value={feed}>{feed}</option>
          ))}
        </select>
      </label>
      <div className="admin-filterbar__actions">
        <button className="button button--primary" type="submit">Apply</button>
        <button
          className="button button--secondary"
          disabled={!controller.state.query || controller.downloading}
          onClick={() => void controller.exportCsv()}
          type="button"
        >
          {controller.downloading ? "Preparing CSV" : "Download CSV"}
        </button>
      </div>
      <p className="admin-filterbar__timezone">{ADMIN_TIME_ZONE} input, UTC `[from, to)` query</p>
      {controller.filterError ? <p className="admin-field-error" role="alert">{controller.filterError}</p> : null}
    </form>
  );
}
function DashboardDataView({ data }: { data: NonNullable<ReturnType<typeof useDashboardController>["state"]["data"]> }) {
  const rows = useMemo(() => chartRows(data.timeseries), [data.timeseries]);
  const feedShare = FEEDS.map((feed) => ({ feed, share: data.feedDiagnostics.feed_share[feed] ?? 0 }));
  const overview = data.overview;
  return (
    <>
      <section aria-labelledby="overview-heading" className="admin-section">
        <div className="admin-section__heading">
          <div><p className="eyebrow">Database aggregate</p><h2 id="overview-heading">Overview</h2></div>
          <span>{formatShanghai(overview.from_utc)} to {formatShanghai(overview.to_utc)}</span>
        </div>
        <div className="admin-metrics">
          <Metric label="Total users" value={formatCount(overview.total_users)} />
          <Metric label="Active users" value={formatCount(overview.active_users)} />
          <Metric label="Requests" value={formatCount(overview.requests)} />
          <Metric label="Exposures" value={formatCount(overview.exposures)} />
          <Metric label="Clicks" value={formatCount(overview.clicks)} />
          <Metric label="CTR" value={formatPercent(overview.ctr)} />
          <Metric label="Likes" value={formatCount(overview.likes)} />
          <Metric label="Shares" value={formatCount(overview.shares)} />
          <Metric label="Revisits" value={formatCount(overview.revisits)} />
          <Metric label="Dwell" value={formatDuration(overview.dwell_ms_total)} />
          <Metric label="Offline" value={formatCount(overview.offline_item_count)} />
          <Metric label="Active model" value={overview.active_model_version ?? "None"} />
        </div>
        {overview.zero_denominator ? (
          <AdminNotice><strong>CTR is 0</strong><span>No exposures exist in this window, so the denominator is zero.</span></AdminNotice>
        ) : null}
      </section>

      <section aria-labelledby="timeseries-heading" className="admin-section">
        <div className="admin-section__heading"><div><p className="eyebrow">Per-page requests</p><h2 id="timeseries-heading">Traffic over time</h2></div></div>
        {rows.length ? (
          <div className="admin-chart" aria-label="Request count time series">
            <ResponsiveContainer height={280} width="100%">
              <LineChart data={rows} margin={{ bottom: 16, left: 0, right: 12, top: 8 }}>
                <CartesianGrid stroke="#d8ddd9" strokeDasharray="3 3" />
                <XAxis dataKey="bucket" minTickGap={24} tick={{ fontSize: 11 }} />
                <YAxis allowDecimals={false} width={42} />
                <Tooltip />
                <Legend />
                {FEEDS.map((feed) => <Line dataKey={feed} key={feed} stroke={FEED_COLORS[feed]} strokeWidth={2} type="monotone" />)}
              </LineChart>
            </ResponsiveContainer>
          </div>
        ) : <p className="admin-muted">No time-series buckets were returned.</p>}
        <div className="admin-table-wrap">
          <table>
            <caption>Accessible time-series values</caption>
            <thead><tr><th>Bucket (Shanghai)</th><th>Feed</th><th>Requests</th><th>Exposures</th><th>Clicks</th><th>Likes</th><th>CTR</th><th>Dwell avg</th></tr></thead>
            <tbody>
              {data.timeseries.map((bucket) => (
                <tr key={`${bucket.bucket_start_utc}:${bucket.feed_type}`}>
                  <td>{formatShanghai(bucket.bucket_start_utc)}</td><td>{bucket.feed_type}</td><td>{bucket.request_count}</td>
                  <td>{bucket.exposure_count}</td><td>{bucket.click_count}</td><td>{bucket.like_count}</td>
                  <td>{formatPercent(bucket.ctr)}</td><td>{formatDuration(bucket.dwell_ms_avg)}</td>
                </tr>
              ))}
              {!data.timeseries.length ? <tr><td colSpan={8}>No buckets in this window.</td></tr> : null}
            </tbody>
          </table>
        </div>
      </section>

      <div className="admin-grid admin-grid--two">
        <section aria-labelledby="feed-share-heading" className="admin-section">
          <div className="admin-section__heading"><div><p className="eyebrow">Request distribution</p><h2 id="feed-share-heading">Feed share</h2></div></div>
          <div className="admin-chart admin-chart--compact" aria-label="Feed request share chart">
            <ResponsiveContainer height={220} width="100%">
              <BarChart data={feedShare}><CartesianGrid stroke="#d8ddd9" strokeDasharray="3 3" /><XAxis dataKey="feed" /><YAxis domain={[0, 1]} tickFormatter={(value) => `${Math.round(Number(value) * 100)}%`} /><Tooltip formatter={(value) => formatPercent(Number(value))} /><Bar dataKey="share" fill="#16836f" /></BarChart>
            </ResponsiveContainer>
          </div>
          <div className="admin-table-wrap"><table><caption>Accessible feed share values</caption><thead><tr><th>Feed</th><th>Share</th></tr></thead><tbody>{feedShare.map((row) => <tr key={row.feed}><td>{row.feed}</td><td>{formatPercent(row.share)}</td></tr>)}</tbody></table></div>
        </section>
        <section aria-labelledby="hot-items-heading" className="admin-section">
          <div className="admin-section__heading"><div><p className="eyebrow">Observed behavior</p><h2 id="hot-items-heading">Hot items</h2></div></div>
          <div className="admin-table-wrap"><table><thead><tr><th>Item</th><th>Title</th><th>Exposures</th><th>Clicks</th><th>Likes</th></tr></thead><tbody>{data.hotItems.map((item) => <tr key={item.item_id}><td><code>{item.item_id}</code></td><td>{item.title}</td><td>{item.exposure_count}</td><td>{item.click_count}</td><td>{item.like_count}</td></tr>)}{!data.hotItems.length ? <tr><td colSpan={5}>No hot items in this window.</td></tr> : null}</tbody></table></div>
        </section>
      </div>

      <section aria-labelledby="feed-diagnostics-heading" className="admin-section">
        <div className="admin-section__heading">
          <div>
            <p className="eyebrow">Server feed aggregates</p>
            <h2 id="feed-diagnostics-heading">Feed diagnostics</h2>
          </div>
        </div>
        <div className="admin-table-wrap">
          <table>
            <thead>
              <tr>
                <th>Feed</th>
                <th>Requests</th>
                <th>Exposures</th>
                <th>Clicks</th>
                <th>Likes</th>
                <th>Shares</th>
                <th>Revisits</th>
                <th>Dwell total</th>
                <th>Dwell average</th>
                <th>CTR</th>
                <th>Active users</th>
              </tr>
            </thead>
            <tbody>
              {data.feedDiagnostics.feeds.map((feed) => (
                <tr key={`${feed.feed_type}:${feed.bucket_start_utc}:${feed.bucket_end_utc}`}>
                  <td>{feed.feed_type}</td>
                  <td>{formatCount(feed.request_count)}</td>
                  <td>{formatCount(feed.exposure_count)}</td>
                  <td>{formatCount(feed.click_count)}</td>
                  <td>{formatCount(feed.like_count)}</td>
                  <td>{formatCount(feed.share_count)}</td>
                  <td>{formatCount(feed.revisit_count)}</td>
                  <td>{formatDuration(feed.dwell_ms_total)}</td>
                  <td>{formatDuration(feed.dwell_ms_avg)}</td>
                  <td>{formatPercent(feed.ctr)}</td>
                  <td>{formatCount(feed.active_user_count)}</td>
                </tr>
              ))}
              {!data.feedDiagnostics.feeds.length ? (
                <tr><td colSpan={11}>No per-feed diagnostics in this window.</td></tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </section>
    </>
  );
}

export function DashboardExperience({ api = adminApi }: { api?: AdminApi }) {
  const controller = useDashboardController(api);
  const { state } = useSession();
  const role = state.status === "authenticated" ? state.user.role : "user";
  return (
    <section className="admin-shell" aria-labelledby="admin-dashboard-title">
      <div className="admin-page-heading"><div><p className="eyebrow">Operations intelligence</p><h1 id="admin-dashboard-title">Dashboard</h1><p>PostgreSQL aggregates, model state and trace evidence.</p></div><button className="button button--ghost" onClick={() => controller.refresh()} type="button">Refresh</button></div>
      <TimeRangeForm controller={controller} />
      {controller.downloadError ? <AdminError error={controller.downloadError} title="CSV download failed" /> : null}
      {controller.state.status === "loading" ? <LoadingState label="Loading dashboard" /> : null}
      {controller.state.error ? <AdminError error={controller.state.error} /> : null}
      {controller.state.status === "empty" && controller.state.data ? <><EmptyState description="The selected database window has no recommendation or behavior activity." title="No activity in this window" /><DashboardDataView data={controller.state.data} /></> : null}
      {(controller.state.status === "ready" || controller.state.status === "refreshing" || (controller.state.status === "error" && controller.state.data)) && controller.state.data ? <DashboardDataView data={controller.state.data} /> : null}
      {controller.state.status === "refreshing" ? <p className="admin-refreshing" role="status">Refreshing current window</p> : null}
      <DiagnosticsPanel api={api} />
      <ModelsPanel api={api} />
      <p className="admin-role-note">Signed-in role: <strong>{role}</strong>. Server authorization remains authoritative.</p>
    </section>
  );
}
