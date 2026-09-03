import type { AdminApi } from "./admin-api";
import { adminApi } from "./admin-api";
import { DashboardExperience } from "./DashboardExperience";
import { OperationsExperience } from "./OperationsExperience";
import { RoleManagementExperience } from "./RoleManagementExperience";

export type AdminView = "dashboard" | "operations" | "roles";

export function AdminExperience({ api = adminApi, view }: { api?: AdminApi; view: AdminView }) {
  if (view === "dashboard") return <DashboardExperience api={api} />;
  if (view === "operations") return <OperationsExperience api={api} />;
  return <RoleManagementExperience api={api} />;
}
