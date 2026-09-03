import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AdminApi } from "./admin-api";
import { ApiError } from "../../api/http";
import { DashboardExperience } from "./DashboardExperience";

const session = vi.hoisted(() => ({ role: "operator_readonly" }));

vi.mock("../../auth/session", () => ({
  useSession: () => ({
    state: {
      status: "authenticated",
      user: { id: "user-1", role: session.role, username: "reader" },
    },
  }),
}));

function api(): AdminApi {
  return {
    compareModels: vi.fn().mockRejectedValue(
      new ApiError("Versions use different windows", {
        code: "NOT_COMPARABLE",
        kind: "api",
        status: 422,
      }),
    ),
    debugRequest: vi.fn().mockResolvedValue({
      candidate_item_ids: ["candidate-1"],
      events: [],
      filtered_item_ids: ["filtered-1"],
      ranked_items: [],
      request_id: "request-1",
    }),
    exportDashboardCsv: vi.fn(),
    getFeedDiagnostics: vi.fn().mockResolvedValue({
      feed_share: { explore: 1 },
      feeds: [
        {
          active_user_count: 17,
          bucket_end_utc: "2026-09-02T00:00:00Z",
          bucket_start_utc: "2026-09-01T00:00:00Z",
          click_count: 23,
          ctr: 0.125,
          dwell_ms_avg: 2345,
          dwell_ms_total: 12345,
          exposure_count: 184,
          feed_type: "explore",
          like_count: 19,
          request_count: 71,
          revisit_count: 13,
          share_count: 11,
        },
      ],
      from_utc: "2026-09-01T00:00:00Z",
      to_utc: "2026-09-02T00:00:00Z",
    }),
    getHotItems: vi.fn().mockResolvedValue([]),
    getOverview: vi.fn().mockResolvedValue({
      active_model_version: null,
      active_users: 0,
      clicks: 0,
      ctr: 0,
      dwell_ms_total: 0,
      exposures: 0,
      from_utc: "2026-09-01T00:00:00Z",
      likes: 0,
      offline_item_count: 0,
      requests: 0,
      revisits: 0,
      shares: 0,
      to_utc: "2026-09-02T00:00:00Z",
      total_users: 0,
      zero_denominator: true,
    }),
    getTimeseries: vi.fn().mockResolvedValue([]),
    listModels: vi.fn().mockResolvedValue([
      {
        activation_eligible: false,
        data_version: "data-1",
        evaluation_comparability: "non_comparable",
        metrics: {},
        model_version: "failed-model",
        published_at: null,
        purpose: "systems_only",
        status: "FAILED",
        trained_at: "2026-09-02T00:00:00Z",
      },
    ]),
    listTrainingJobs: vi.fn().mockResolvedValue([]),
  } as unknown as AdminApi;
}

describe("DashboardExperience", () => {
  beforeEach(() => {
    session.role = "operator_readonly";
  });

  it("renders empty and zero-denominator states without inventing CTR", async () => {
    render(<DashboardExperience api={api()} />);

    expect(await screen.findByText("No activity in this window")).toBeTruthy();
    expect(screen.getByText("No exposures exist in this window, so the denominator is zero.")).toBeTruthy();
    expect(screen.getAllByText("0%").length).toBeGreaterThan(0);
  });

  it("keeps FAILED models never-active and a comparison 422 visible", async () => {
    render(<DashboardExperience api={api()} />);

    expect(await screen.findByText("failed-model")).toBeTruthy();
    expect(screen.getByText("Never active")).toBeTruthy();
    expect(screen.getByText("Model comparison unavailable")).toBeTruthy();
    expect(screen.getByText("Versions use different windows")).toBeTruthy();
  });

  it("states unavailable candidate scores, sources and filter reasons honestly", async () => {
    render(<DashboardExperience api={api()} />);
    fireEvent.change(screen.getByLabelText("Request UUID"), { target: { value: "request-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Inspect trace" }));

    await waitFor(() => expect(screen.getByText("candidate-1")).toBeTruthy());
    expect(screen.getByText("Source unavailable · Score unavailable")).toBeTruthy();
    expect(screen.getByText("Filter reason unavailable")).toBeTruthy();
    expect(document.body.innerHTML).not.toContain("dangerouslySetInnerHTML");
  });

  it("renders every per-feed diagnostic field returned by the API", async () => {
    render(<DashboardExperience api={api()} />);
    const heading = await screen.findByRole("heading", { name: "Feed diagnostics" });
    const section = heading.closest("section");
    expect(section).toBeTruthy();
    const row = within(section!).getByRole("cell", { name: "explore" }).closest("tr");
    expect(row).toBeTruthy();
    const cells = within(row!).getAllByRole("cell").map((cell) => cell.textContent);
    expect(cells).toEqual([
      "explore",
      "71",
      "184",
      "23",
      "19",
      "11",
      "13",
      "12.3 s",
      "2.3 s",
      "12.5%",
      "17",
    ]);
    expect(
      screen
        .getByRole("table", { name: "Accessible feed share values" })
        .parentElement?.classList.contains("admin-table-wrap"),
    ).toBe(true);
  });
});
