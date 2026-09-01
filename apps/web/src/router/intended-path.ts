const BLOCKED_AUTH_PATHS = new Set(["/login", "/register"]);

export function getSafeIntendedPath(value: string | null | undefined): string {
  if (!value || !value.startsWith("/") || value.startsWith("//")) return "/";
  if (/[\u0000-\u001f\u007f]/.test(value)) return "/";

  try {
    const parsed = new URL(value, "https://microlens.local");
    if (parsed.origin !== "https://microlens.local") return "/";
    if (BLOCKED_AUTH_PATHS.has(parsed.pathname)) return "/";
    return `${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return "/";
  }
}
