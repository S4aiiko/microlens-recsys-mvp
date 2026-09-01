import type { Role } from "../api/generated";

export const CAPABILITIES = {
  dashboardRead: ["operator_readonly", "operator", "admin"],
  feedAndProfile: ["user", "operator_readonly", "operator", "admin"],
  modelPublish: ["admin"],
  operationsWrite: ["operator", "admin"],
  roleManagement: ["admin"],
} as const satisfies Record<string, readonly Role[]>;

export type Capability = keyof typeof CAPABILITIES;

export function hasCapability(role: Role, capability: Capability): boolean {
  return (CAPABILITIES[capability] as readonly Role[]).includes(role);
}
