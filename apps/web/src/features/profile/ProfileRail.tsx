import type { UserProfileResponse } from "../../api/generated";
import "./profile.css";

export type ProfileRailState = "ready" | "loading" | "empty" | "error" | "offline";

export interface ProfileRailError {
  message: string;
  requestId?: string | null;
}

export interface ProfileRailProps {
  error?: ProfileRailError;
  onRetry?: () => void;
  profile?: UserProfileResponse | null;
  state: ProfileRailState;
}

const SUMMARY_SECTIONS: ReadonlyArray<{
  key: keyof Pick<
    UserProfileResponse,
    | "positive_summary"
    | "negative_summary"
    | "dwell_summary"
    | "revisit_summary"
    | "share_summary"
    | "title_preferences"
  >;
  title: string;
}> = [
  { key: "positive_summary", title: "Positive signals" },
  { key: "negative_summary", title: "Not interested" },
  { key: "dwell_summary", title: "Dwell" },
  { key: "revisit_summary", title: "Revisits" },
  { key: "share_summary", title: "Shares" },
  { key: "title_preferences", title: "Title preferences" },
];

function readableKey(key: string): string {
  return key.replaceAll("_", " ");
}

function readableValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "Not available";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return "None yet";
    const primitives = value.filter(
      (entry): entry is string | number | boolean =>
        typeof entry === "string" || typeof entry === "number" || typeof entry === "boolean",
    );
    return primitives.length === value.length ? primitives.join(", ") : `${value.length} entries`;
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length === 0) return "None yet";
    const primitiveEntries = entries.filter(([, nested]) =>
      ["string", "number", "boolean"].includes(typeof nested),
    );
    if (primitiveEntries.length > 0) {
      return primitiveEntries
        .slice(0, 3)
        .map(([key, nested]) => `${readableKey(key)}: ${String(nested)}`)
        .join("; ");
    }
    return `${entries.length} fields`;
  }
  return "Not available";
}

function ProfileSkeleton() {
  return (
    <div className="ml-profile__skeleton" aria-hidden="true">
      {[0, 1, 2, 3].map((key) => (
        <span key={key} />
      ))}
    </div>
  );
}

function ProfileState({
  error,
  onRetry,
  state,
}: Pick<ProfileRailProps, "error" | "onRetry" | "state">) {
  if (state === "loading") {
    return (
      <div className="ml-profile__state" aria-busy="true" aria-live="polite" role="status">
        <span>Loading profile</span>
        <ProfileSkeleton />
      </div>
    );
  }

  if (state === "empty") {
    return (
      <div className="ml-profile__state ml-profile__state--empty">
        <strong>No profile signals yet</strong>
        <p>Interactions will appear here after the server accepts them.</p>
      </div>
    );
  }

  if (state === "offline" || state === "error") {
    return (
      <div className="ml-profile__state ml-profile__state--error" role="alert">
        <strong>{state === "offline" ? "Profile unavailable offline" : "Profile unavailable"}</strong>
        <p>
          {state === "offline"
            ? "Reconnect to refresh your latest signals."
            : (error?.message ?? "The profile request could not be completed.")}
        </p>
        {error?.requestId ? <code>Request {error.requestId}</code> : null}
        {onRetry ? (
          <button onClick={onRetry} type="button">
            Retry
          </button>
        ) : null}
      </div>
    );
  }

  return null;
}

function SummarySection({
  summary,
  title,
}: {
  summary: Record<string, unknown>;
  title: string;
}) {
  const entries = Object.entries(summary);

  return (
    <section className="ml-profile__section">
      <h3>{title}</h3>
      {entries.length === 0 ? (
        <p className="ml-profile__quiet">None yet</p>
      ) : (
        <dl>
          {entries.slice(0, 5).map(([key, value]) => (
            <div key={key}>
              <dt>{readableKey(key)}</dt>
              <dd>{readableValue(value)}</dd>
            </div>
          ))}
        </dl>
      )}
    </section>
  );
}

export function ProfileRail({ error, onRetry, profile, state }: ProfileRailProps) {
  const showProfile = profile && (state === "ready" || state === "offline" || state === "error");

  return (
    <section
      className="ml-profile"
      data-state={state}
      aria-labelledby="ml-profile-title"
    >
      <header className="ml-profile__header">
        <div>
          <p>Live signals</p>
          <h2 id="ml-profile-title">Your profile</h2>
        </div>
        {profile ? <span>v{profile.profile_version}</span> : null}
      </header>

      {state !== "ready" ? <ProfileState error={error} onRetry={onRetry} state={state} /> : null}

      {showProfile ? (
        <div className="ml-profile__content">
          <div className="ml-profile__identity">
            <span>User</span>
            <code>{profile.user_id}</code>
            <span>Updated</span>
            <time dateTime={profile.updated_at}>{profile.updated_at}</time>
          </div>

          <section className="ml-profile__section">
            <h3>Recent interactions</h3>
            {profile.recent_interactions.length === 0 ? (
              <p className="ml-profile__quiet">No recent interactions</p>
            ) : (
              <ol className="ml-profile__recent">
                {profile.recent_interactions.slice(0, 5).map((interaction, index) => (
                  <li key={`${index}:${readableValue(interaction)}`}>
                    {readableValue(interaction)}
                  </li>
                ))}
              </ol>
            )}
          </section>

          {SUMMARY_SECTIONS.map((section) => (
            <SummarySection
              key={section.key}
              summary={profile[section.key]}
              title={section.title}
            />
          ))}
        </div>
      ) : null}
    </section>
  );
}
