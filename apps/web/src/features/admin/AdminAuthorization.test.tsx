import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AdminItem, Role, User } from "../../api/generated";
import { ApiError } from "../../api/http";
import type { AdminApi } from "./admin-api";
import { OperationsExperience } from "./OperationsExperience";
import { RoleManagementExperience } from "./RoleManagementExperience";

const session = vi.hoisted(() => ({ role: "operator_readonly" as Role }));

vi.mock("../../auth/session", () => ({
  useSession: () => ({
    state: {
      status: "authenticated",
      user: { id: "actor-1", role: session.role, username: "actor" },
    },
  }),
}));

const ITEM: AdminItem = {
  cover: null,
  heat: 3,
  item_id: "item-1",
  online_status: "online",
  state_version: 2,
  title: "Item one",
  updated_at: "2026-09-02T00:00:00Z",
};

const SECOND_ITEM: AdminItem = {
  ...ITEM,
  item_id: "item-2",
  state_version: 5,
  title: "Item two",
};

const USER: User = {
  created_at: "2026-09-02T00:00:00Z",
  id: "user-1",
  role: "user",
  status: "enabled",
  username: "member",
};

function readApi(overrides: Partial<AdminApi> = {}): AdminApi {
  return {
    listOperations: vi.fn().mockResolvedValue([]),
    searchItems: vi.fn().mockResolvedValue([ITEM]),
    ...overrides,
  } as unknown as AdminApi;
}

describe("admin authorization experiences", () => {
  beforeEach(() => {
    session.role = "operator_readonly";
  });

  it("shows read-only item and audit data without write controls", async () => {
    render(<OperationsExperience api={readApi()} />);
    expect(await screen.findByText("Item one")).toBeTruthy();
    expect(screen.getByText("Read-only operations view")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Submit operation" })).toBeNull();
    expect(screen.getByText("Operations audit")).toBeTruthy();
  });

  it("shows operator write controls and a server-authoritative 403", async () => {
    session.role = "operator";
    const api = readApi({
      createPromotion: vi.fn().mockRejectedValue(
        new ApiError("Policy denied this operation", {
          code: "FORBIDDEN",
          kind: "forbidden",
          status: 403,
        }),
      ),
    });
    render(<OperationsExperience api={api} />);
    fireEvent.click(await screen.findByRole("checkbox", { name: "Select Item one" }));
    fireEvent.change(screen.getByLabelText("Reason"), { target: { value: "manual test" } });
    fireEvent.click(screen.getByRole("button", { name: "Submit operation" }));

    expect(await screen.findByText("Server denied this action")).toBeTruthy();
    expect(screen.getByText("Policy denied this operation")).toBeTruthy();
  });

  it("keeps selected item snapshots across searches and submits every target", async () => {
    session.role = "operator";
    const searchItems = vi.fn().mockResolvedValueOnce([ITEM]).mockResolvedValue([SECOND_ITEM]);
    const createPromotion = vi.fn().mockResolvedValue({
      batch_id: "batch-1",
      created_at: "2026-09-02T00:00:00Z",
      expected_state_version: 5,
      scheduled_at: null,
      status: "succeeded",
    });
    render(<OperationsExperience api={readApi({ createPromotion, searchItems })} />);

    fireEvent.click(await screen.findByRole("checkbox", { name: "Select Item one" }));
    fireEvent.change(screen.getByLabelText("ID or title"), { target: { value: "item-2" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    fireEvent.click(await screen.findByRole("checkbox", { name: "Select Item two" }));

    expect(screen.getAllByText("2 / 100 selected").length).toBeGreaterThan(0);
    expect(screen.getByText("Item one / state v2")).toBeTruthy();
    expect(screen.getByText("Item two / state v5")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Reason"), { target: { value: "cross-search" } });
    fireEvent.click(screen.getByRole("button", { name: "Submit operation" }));

    await waitFor(() => expect(createPromotion).toHaveBeenCalledOnce());
    expect(createPromotion).toHaveBeenCalledWith(expect.objectContaining({
      targets: ["item-1", "item-2"],
    }));
  });

  it("does not request users for a non-admin role", () => {
    const listUsers = vi.fn();
    render(<RoleManagementExperience api={readApi({ listUsers })} />);
    expect(screen.getByText("Administrator access required")).toBeTruthy();
    expect(listUsers).not.toHaveBeenCalled();
  });

  it("lists and updates roles for admin", async () => {
    session.role = "admin";
    const listUsers = vi.fn().mockResolvedValue([USER]);
    const updateRole = vi.fn().mockResolvedValue({ ...USER, role: "operator" });
    render(<RoleManagementExperience api={readApi({ listUsers, updateRole })} />);

    const roleSelect = await screen.findByRole("combobox", { name: "Role for member" });
    fireEvent.change(roleSelect, { target: { value: "operator" } });
    await waitFor(() => expect(updateRole).toHaveBeenCalledWith("user-1", "operator"));
    expect((roleSelect as HTMLSelectElement).value).toBe("operator");
  });

  it("keeps a role-management server 403 visible", async () => {
    session.role = "admin";
    const listUsers = vi.fn().mockRejectedValue(
      new ApiError("Admin permission revoked", {
        code: "FORBIDDEN",
        kind: "forbidden",
        status: 403,
      }),
    );
    render(<RoleManagementExperience api={readApi({ listUsers })} />);
    expect(await screen.findByText("Server denied this action")).toBeTruthy();
    expect(screen.getByText("Admin permission revoked")).toBeTruthy();
  });
});
