import { describe, expect, it } from "vitest";
import type { Role } from "../api/generated";
import { hasCapability } from "./capabilities";

const roles: Role[] = ["user", "operator_readonly", "operator", "admin"];

describe("frozen four-role capability matrix", () => {
  it.each([
    ["user", true, false, false, false, false],
    ["operator_readonly", true, true, false, false, false],
    ["operator", true, true, true, false, false],
    ["admin", true, true, true, true, true],
  ] as const)(
    "%s maps to the exact frozen capabilities",
    (role, feed, dashboard, operations, publish, rolesManagement) => {
      expect(roles).toContain(role);
      expect(hasCapability(role, "feedAndProfile")).toBe(feed);
      expect(hasCapability(role, "dashboardRead")).toBe(dashboard);
      expect(hasCapability(role, "operationsWrite")).toBe(operations);
      expect(hasCapability(role, "modelPublish")).toBe(publish);
      expect(hasCapability(role, "roleManagement")).toBe(rolesManagement);
    },
  );
});
