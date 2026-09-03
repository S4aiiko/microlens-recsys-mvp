import { TZDate } from "@date-fns/tz";
import type { FeedType } from "../../api/generated";
import type { AdminApi, DashboardQuery } from "./admin-api";

export const ADMIN_TIME_ZONE = "Asia/Shanghai";

export interface DashboardFilters {
  feedType: FeedType | "all";
  fromLocal: string;
  toLocal: string;
}

const LOCAL_DATE_TIME =
  /^(?<year>\d{4})-(?<month>\d{2})-(?<day>\d{2})T(?<hour>\d{2}):(?<minute>\d{2})(?::(?<second>\d{2}))?$/;

export function shanghaiLocalToUtc(value: string): string {
  const match = LOCAL_DATE_TIME.exec(value);
  if (!match?.groups) throw new Error("Enter a complete date and time.");
  const year = Number(match.groups.year);
  const month = Number(match.groups.month);
  const day = Number(match.groups.day);
  const hour = Number(match.groups.hour);
  const minute = Number(match.groups.minute);
  const second = Number(match.groups.second ?? 0);
  const date = new TZDate(year, month - 1, day, hour, minute, second, ADMIN_TIME_ZONE);
  if (
    date.getFullYear() !== year ||
    date.getMonth() !== month - 1 ||
    date.getDate() !== day ||
    date.getHours() !== hour ||
    date.getMinutes() !== minute ||
    date.getSeconds() !== second
  ) {
    throw new Error("Enter a valid Asia/Shanghai date and time.");
  }
  return new Date(date.getTime()).toISOString();
}

export function utcToShanghaiLocal(value: string | number | Date): string {
  const date = new TZDate(new Date(value), ADMIN_TIME_ZONE);
  const pad = (part: number) => String(part).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`;
}

export function formatShanghai(value: string | null): string {
  if (!value) return "Not available";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Invalid timestamp";
  return new Intl.DateTimeFormat("en-GB", {
    dateStyle: "medium",
    timeStyle: "medium",
    timeZone: ADMIN_TIME_ZONE,
  }).format(date);
}

export function dashboardQuery(filters: DashboardFilters): DashboardQuery {
  const fromUtc = shanghaiLocalToUtc(filters.fromLocal);
  const toUtc = shanghaiLocalToUtc(filters.toLocal);
  if (Date.parse(fromUtc) >= Date.parse(toUtc)) {
    throw new Error("The start must be before the end for the [from, to) window.");
  }
  return {
    feedType: filters.feedType === "all" ? null : filters.feedType,
    fromUtc,
    toUtc,
  };
}

export function defaultDashboardFilters(now: Date = new Date()): DashboardFilters {
  return {
    feedType: "all",
    fromLocal: utcToShanghaiLocal(now.getTime() - 24 * 60 * 60 * 1000),
    toLocal: utcToShanghaiLocal(now),
  };
}

export interface DownloadEnvironment {
  createObjectURL(blob: Blob | File): string;
  revokeObjectURL(url: string): void;
  trigger(url: string, filename: string): void;
}

export function browserDownloadEnvironment(): DownloadEnvironment {
  return {
    createObjectURL: (blob) => URL.createObjectURL(blob),
    revokeObjectURL: (url) => URL.revokeObjectURL(url),
    trigger(url, filename) {
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      anchor.style.display = "none";
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
    },
  };
}

export async function downloadDashboardCsv(
  api: AdminApi,
  query: DashboardQuery,
  environment: DownloadEnvironment = browserDownloadEnvironment(),
): Promise<void> {
  const blob = await api.exportDashboardCsv(query);
  const url = environment.createObjectURL(blob);
  try {
    environment.trigger(url, `microlens-dashboard-${query.fromUtc.slice(0, 10)}.csv`);
  } finally {
    environment.revokeObjectURL(url);
  }
}
