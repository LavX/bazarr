import { Text } from "@mantine/core";
import type {
  DiscoverContext,
  DiscoverProviderOutcome,
} from "@/types/discover";
import styles from "./Discover.module.scss";

const outcomeLabels: Record<DiscoverProviderOutcome["status"], string> = {
  success: "Search complete",
  empty: "No matches",
  unverified:
    "No results returned for this unverified query. Provider query support could not be confirmed.",
  ["authentication_required"]: "Sign in to this provider in provider settings",
  ["setup_required"]: "Provider setup required",
  cooldown: "Provider is cooling down",
  unreachable: "Provider could not be reached",
  timeout: "Provider search timed out",
  abandoned: "Still searching when the search deadline passed",
  ["not_started"]:
    "Not searched: the search ran out of time before this provider started",
  error: "Provider search failed",
  skipped: "Provider could not search this target",
  saturated: "Search capacity is busy. Try again shortly",
};

// Each reason names what actually happened to that provider. A built-in
// provider that Discover cannot use is enabled and was skipped, so it is not
// told to be enabled; it is told why it was skipped.
const skipLabels: Record<string, string> = {
  ["missing_configuration"]:
    "Required settings are missing. Configure this provider in the Subtitle Hub.",
  ["automated_requests_blocked"]: "Website blocked automated requests",
  ["requires_file"]:
    "Extracts subtitles from a local video. Select a library copy to use this provider.",
  ["not_catalog_provider"]:
    "Built-in provider, skipped. Discover searches trusted catalog providers only.",
  ["provider_unavailable"]:
    "Not installed or not loaded, skipped. Check it in the Subtitle Hub.",
  ["rate_limited"]: "Too many requests were sent to this provider recently",
  ["download_limit_reached"]: "This provider's download limit has been reached",
  ["search_limit_reached"]: "This provider's search limit has been reached",
};

// A cooling-down provider is usually one this search did not ask at all. The
// reason names what happened on the search that put it on the wait, which is
// how the reader tells a slow site from a broken sign-in.
const cooldownCauses: Record<string, string> = {
  timeout: "Timed out on the last search",
  ["wall_timeout"]: "Did not finish in time on the last search",
  error: "Site error on the last search",
  unreachable: "Could not be reached on the last search",
  capacity: "Search capacity was busy on the last search",
  ["authentication_required"]:
    "Sign-in failed on the last search. Check the provider settings.",
  ["setup_required"]:
    "Setup was incomplete on the last search. Check the provider settings.",
};

function CooldownCause({ provider }: { provider: DiscoverProviderOutcome }) {
  const reason = provider.reason ?? "";
  const cause =
    provider.status === "cooldown"
      ? (cooldownCauses[reason] ?? skipLabels[reason])
      : undefined;
  return cause ? (
    <Text size="sm" c="dimmed">
      {cause}
    </Text>
  ) : null;
}

const languageNames = new Intl.DisplayNames(["en"], { type: "language" });
function languageName(code: string) {
  try {
    return languageNames.of(code) ?? code;
  } catch {
    return code;
  }
}
function outcomeLabel(
  provider: DiscoverProviderOutcome,
  context: DiscoverContext,
) {
  // A wait, whatever caused it. The cause is shown beneath this line, not in
  // place of it.
  if (provider.status === "cooldown") return outcomeLabels.cooldown;
  const language = languageName(context.language);
  if (provider.reason === "unsupported_language")
    return `This provider does not offer ${language} subtitles.`;
  if (provider.reason === "excluded_language")
    return `${language} is excluded in this provider's settings.`;
  if (provider.reason === "unsupported_media")
    return `This provider does not search ${context.mode === "release" ? "this release format" : context.media_type === "movie" ? "movies" : "TV episodes"}.`;
  return skipLabels[provider.reason ?? ""] ?? outcomeLabels[provider.status];
}

export default function ProviderCoverage({
  providers,
  context,
}: {
  providers: DiscoverProviderOutcome[];
  context: DiscoverContext;
}) {
  if (!providers.length) return null;
  const searched = providers.filter(
    (p) => p.status === "success" || p.status === "empty",
  );
  const skipped = providers.filter((p) => p.status === "skipped");
  const unverified = providers.filter((p) => p.status === "unverified");
  // The provider answered nothing, but nothing is known to be wrong with it
  // either: this search simply ran out of its own time. Grouping that under
  // "unavailable" reports our scheduling as the provider's fault.
  const outOfTime = providers.filter(
    (p) => p.status === "not_started" || p.status === "abandoned",
  );
  const unavailable = providers.filter(
    (p) =>
      !["success", "empty", "skipped", "unverified"].includes(p.status) &&
      !outOfTime.includes(p),
  );
  const groups = [
    { label: "Providers searched", rows: searched, expanded: false },
    { label: "Unverified searches", rows: unverified, expanded: false },
    { label: "Providers unavailable", rows: unavailable, expanded: true },
    { label: "Not finished in time", rows: outOfTime, expanded: true },
    { label: "Not applicable to this search", rows: skipped, expanded: false },
  ];
  return (
    <details className={styles.providerDisclosure}>
      <summary id="discover-coverage">
        Search details{" "}
        <span className={styles.providerSummary}>
          · {searched.length} searched
          {unverified.length ? ` · ${unverified.length} unverified` : ""}
          {skipped.length ? ` · ${skipped.length} skipped` : ""}
          {outOfTime.length ? ` · ${outOfTime.length} out of time` : ""}
          {unavailable.length ? ` · ${unavailable.length} unavailable` : ""}
        </span>
      </summary>
      {groups
        .filter((group) => group.rows.length)
        .map((group) => (
          <details
            key={group.label}
            className={styles.providerGroup}
            open={group.expanded}
          >
            <summary>
              {group.label} ({group.rows.length})
            </summary>
            {group.rows === skipped && (
              <Text size="sm" c="dimmed">
                These providers were not used for this selection.
              </Text>
            )}
            <ul className={styles.providerList}>
              {group.rows.map((provider) => (
                <li key={provider.provider}>
                  <div>
                    <Text fw={600}>{provider.provider}</Text>
                    <Text size="sm">{outcomeLabel(provider, context)}</Text>
                    <CooldownCause provider={provider} />
                    {provider.supported_media?.length ? (
                      <Text size="sm" c="dimmed">
                        Searches{" "}
                        {provider.supported_media
                          .map((type) =>
                            type === "movie" ? "movies" : "TV episodes",
                          )
                          .join(" and ")}
                        .
                      </Text>
                    ) : null}
                    {provider.supported_languages?.length ? (
                      <Text size="sm" c="dimmed">
                        Languages:{" "}
                        {provider.supported_languages
                          .map(languageName)
                          .join(", ")}
                        .
                      </Text>
                    ) : null}
                    {provider.retry_at && (
                      <Text size="sm" c="dimmed">
                        Retry after{" "}
                        <time dateTime={provider.retry_at}>
                          {new Date(provider.retry_at).toLocaleTimeString([], {
                            hour: "2-digit",
                            minute: "2-digit",
                          })}
                        </time>
                      </Text>
                    )}
                  </div>
                  {provider.status === "success" && (
                    <Text size="sm">
                      {provider.result_count}{" "}
                      {provider.result_count === 1 ? "result" : "results"}
                    </Text>
                  )}
                </li>
              ))}
            </ul>
          </details>
        ))}
    </details>
  );
}
