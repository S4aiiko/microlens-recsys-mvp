import { describe, expect, it } from "vitest";
import { getSafeIntendedPath } from "./intended-path";

describe("safe intended path restoration", () => {
  it("keeps internal path, query and hash", () => {
    expect(getSafeIntendedPath("/operations?tab=scheduled#next")).toBe(
      "/operations?tab=scheduled#next",
    );
  });

  it.each([
    "https://attacker.test/steal",
    "//attacker.test/steal",
    "/login",
    "/register?from=/operations",
    "/safe\u0000unsafe",
    "not-a-path",
    "",
  ])("rejects unsafe or auth-loop destination %s", (value) => {
    expect(getSafeIntendedPath(value)).toBe("/");
  });
});
