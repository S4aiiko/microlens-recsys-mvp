import type { Role } from "../../api/generated";
import { useSession } from "../../auth/session";
import type { AdminApi } from "./admin-api";
import { adminApi } from "./admin-api";
import { AdminError, AdminNotice, StatusBadge } from "./AdminPrimitives";
import { formatShanghai } from "./admin-time";
import { useRoleManagementController } from "./useOperationsController";
import "./admin.css";

const ROLES: Role[] = ["user", "operator_readonly", "operator", "admin"];

export function RoleManagementExperience({ api = adminApi }: { api?: AdminApi }) {
  const { state } = useSession();
  const role: Role = state.status === "authenticated" ? state.user.role : "user";
  const controller = useRoleManagementController(api, role);

  return (
    <section className="admin-shell" aria-labelledby="admin-roles-title">
      <div className="admin-page-heading">
        <div><p className="eyebrow">Administrator control</p><h1 id="admin-roles-title">Role management</h1><p>Account roles are loaded from and updated by the admin API.</p></div>
        {role === "admin" ? <button className="button button--ghost" onClick={() => void controller.refresh()} type="button">Refresh</button> : null}
      </div>
      {role !== "admin" ? (
        <AdminNotice><strong>Administrator access required</strong><span>This page does not request or infer a user list for role <code>{role}</code>. A direct server request would still be subject to 403 authorization.</span></AdminNotice>
      ) : null}
      {controller.error ? <AdminError error={controller.error} /> : null}
      {role === "admin" ? (
        <div className="admin-table-wrap">
          <table>
            <thead><tr><th>User</th><th>Username</th><th>Status</th><th>Created</th><th>Role</th></tr></thead>
            <tbody>
              {controller.users.map((user) => (
                <tr key={user.id}>
                  <td><code>{user.id}</code></td>
                  <td>{user.username}</td>
                  <td><StatusBadge value={user.status} /></td>
                  <td>{formatShanghai(user.created_at)}</td>
                  <td>
                    <select
                      aria-label={`Role for ${user.username}`}
                      disabled={controller.updatingId === user.id}
                      onChange={(event) => void controller.update(user.id, event.target.value as Role)}
                      value={user.role}
                    >
                      {ROLES.map((candidate) => <option key={candidate} value={candidate}>{candidate}</option>)}
                    </select>
                  </td>
                </tr>
              ))}
              {!controller.users.length && !controller.loading ? <tr><td colSpan={5}>No users returned.</td></tr> : null}
              {controller.loading ? <tr><td colSpan={5}>Loading users</td></tr> : null}
            </tbody>
          </table>
        </div>
      ) : null}
      <p className="admin-role-note">Signed-in role: <strong>{role}</strong>. Browser controls are only a usability boundary; server authorization is authoritative.</p>
    </section>
  );
}
