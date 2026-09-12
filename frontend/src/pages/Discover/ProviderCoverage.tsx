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
  authentication_required: "Sign in to this provider in provider settings",
  setup_required: "Provider setup required",
  cooldown: "Provider is cooling down",
  unreachable: "Provider could not be reached",
  timeout: "Provider search timed out",
  error: "Provider search failed",
  skipped: "Provider could not search this target",
  saturated: "Search capacity is busy. Try again shortly",
};

// Each reason names what actually happened to that provider. A built-in
// provider that Discover cannot use is enabled and was skipped, so it is not
// told to be enabled; it is told why it was skipped.
const skipLabels: Record<string, string> = {
  missing_configuration:
    "Required settings are missing. Configure this provider in the Subtitle Hub.",
  automated_requests_blocked: "Website blocked automated requests",
  requires_file:
    "Extracts subtitles from a local video. Select a library copy to use this provider.",
  not_catalog_provider:
    "Built-in provider, skipped. Discover searches trusted catalog providers only.",
  provider_unavailable:
    "Not installed or not loaded, skipped. Check it in the Subtitle Hub.",
};

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
  const unavailable = providers.filter(
    (p) => !["success", "empty", "skipped", "unverified"].includes(p.status),
  );
  const groups = [
    { label: "Providers searched", rows: searched, expanded: false },
    { label: "Unverified searches", rows: unverified, expanded: false },
    { label: "Providers unavailable", rows: unavailable, expanded: true },
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
