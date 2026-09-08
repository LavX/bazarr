import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation } from "react-router";
import { Alert, Anchor, Button, Group, Text, Title } from "@mantine/core";
import { faArrowRight, faFilm } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useDiscoverTrending } from "@/apis/hooks/discover";
import { useDiscover } from "@/contexts/Discover";
import type { TrendingMediaType, TrendingTitle } from "@/types/discover";
import styles from "./Discover.module.scss";

const filters: { value: TrendingMediaType; label: string }[] = [
  { value: "all", label: "All media" },
  { value: "movie", label: "Movies" },
  { value: "series", label: "Series" },
];

function Artwork({
  src,
  poster = false,
}: {
  src: string | null;
  poster?: boolean;
}) {
  const [failed, setFailed] = useState(false);
  if (!src || failed)
    return poster ? (
      <span className={styles.missingArt}>
        <FontAwesomeIcon icon={faFilm} />
        <span>Artwork unavailable</span>
      </span>
    ) : null;
  return (
    <img
      src={src}
      alt=""
      loading={poster ? "lazy" : "eager"}
      decoding="async"
      onError={() => setFailed(true)}
    />
  );
}

export default function Trending() {
  const { state, updateBrowsing, updateDraft } = useDiscover();
  const { browsing } = state;
  const feed = useDiscoverTrending(browsing.trendingFilter);
  const location = useLocation();
  const [clock, setClock] = useState(Date.now);
  const data = feed.data;
  const expired = Boolean(
    data?.stale_until && Date.parse(data.stale_until) <= clock,
  );
  useEffect(() => {
    if (!data?.stale_until) return;
    const timers = [data.expires_at, data.stale_until]
      .filter((value): value is string => Boolean(value))
      .map((value) =>
        window.setTimeout(
          () => setClock(Date.now()),
          Math.max(0, Date.parse(value) - Date.now()) + 1,
        ),
      );
    return () => timers.forEach((timer) => window.clearTimeout(timer));
  }, [data?.expires_at, data?.stale_until]);
  const items = useMemo(
    () => (expired ? [] : (data?.items ?? [])),
    [expired, data?.items],
  );
  const featured = items[0];
  const restored = useRef(false);
  useEffect(() => {
    if (restored.current || !browsing.focusId.startsWith("discover-trending-"))
      return;
    const opener = document.getElementById(browsing.focusId);
    if (opener) {
      restored.current = true;
      opener.focus({ preventScroll: true });
      window.scrollTo({ top: browsing.scrollY, behavior: "instant" });
    }
  }, [items, browsing.focusId, browsing.scrollY]);
  const open = (item: TrendingTitle, focusId: string) => {
    const kind = item.media_type === "series" ? "show" : "movie";
    const reopen =
      browsing.identityLoaded && browsing.adoptedSourceId === item.source_id;
    updateBrowsing({
      selectedSource: "tmdb",
      selectedId: item.id,
      selectedType: kind,
      selectedSeason: reopen ? browsing.selectedSeason : null,
      selectedEpisode: reopen ? browsing.selectedEpisode : null,
      adoptedSourceId: reopen ? item.source_id : null,
      identityLoaded: reopen,
      adoptedMovieId: reopen && kind === "movie" ? item.id : null,
      focusId,
      scrollY: window.scrollY,
      returnTarget: location.pathname + location.search + location.hash,
    });
    if (!reopen)
      updateDraft({
        mode: "title",
        mediaType: kind === "show" ? "episode" : "movie",
        showId: kind === "show" ? item.id : undefined,
        showTvdbId: undefined,
        imdbId: "",
        title: item.title,
        year: item.year ?? undefined,
        episodeIdentity: undefined,
        manualConfirmed: false,
        manualEntry: false,
        season: "",
        episode: "",
      });
  };
  const failed =
    (feed.isSuccess && !data) ||
    feed.settingsError ||
    feed.isError ||
    expired ||
    data?.status === "unavailable";
  const setup =
    !feed.settingsLoading &&
    !feed.settingsError &&
    (!feed.configured ||
      data?.status === "unconfigured" ||
      data?.status === "authentication_failed");
  return (
    <section aria-labelledby="trending-title" className={styles.trending}>
      <Group className={styles.trendingFilters} justify="space-between">
        <div role="group" aria-label="Global trending media">
          {filters.map((filter) => (
            <button
              key={filter.value}
              type="button"
              aria-pressed={browsing.trendingFilter === filter.value}
              onClick={() =>
                updateBrowsing({
                  trendingFilter: filter.value,
                  focusId: `discover-trending-filter-${filter.value}`,
                })
              }
              id={`discover-trending-filter-${filter.value}`}
            >
              {filter.label}
            </button>
          ))}
        </div>
        <Text size="xs">Global catalog · Weekly</Text>
      </Group>
      {featured && (
        <article className={styles.feature}>
          <Artwork key={featured.backdrop_url} src={featured.backdrop_url} />
          <div className={styles.featureCopy}>
            <Title order={2}>{featured.title}</Title>
            <Text>
              {featured.year ?? "Year unavailable"} ·{" "}
              {featured.media_type === "series" ? "Series" : "Film"}
            </Text>
            {featured.overview && (
              <Text className={styles.featureOverview} lineClamp={2}>
                {featured.overview}
              </Text>
            )}
            <Button
              id={`discover-trending-feature-${featured.source_id}`}
              aria-label={`Explore ${featured.title}`}
              rightSection={<FontAwesomeIcon icon={faArrowRight} />}
              onClick={() =>
                open(
                  featured,
                  `discover-trending-feature-${featured.source_id}`,
                )
              }
            >
              {featured.media_type === "series"
                ? "Explore series"
                : "Explore film"}
            </Button>
          </div>
        </article>
      )}
      <Group className={styles.railHeading} justify="space-between">
        <Title order={2} id="trending-title">
          Trending this week
        </Title>
        {feed.configured && (
          <Button
            variant="subtle"
            loading={feed.isFetching}
            onClick={() => void feed.refetch()}
          >
            Refresh trending
          </Button>
        )}
      </Group>
      <div role="status" aria-live="polite" className={styles.feedStatus}>
        {(feed.settingsLoading || feed.isFetching) && (
          <Text size="sm">Loading weekly trending titles.</Text>
        )}
        {setup && (
          <Alert
            color="yellow"
            title={
              data?.status === "authentication_failed"
                ? "TMDB access needs attention"
                : "Explore beyond your library"
            }
          >
            <Text size="sm">
              {data?.status === "authentication_failed"
                ? "TMDB rejected the saved access token."
                : "Connect TMDB for global films, series and artwork. Library connections are optional."}
            </Text>
            <Anchor
              component={Link}
              to="/settings/discover"
              className={styles.settingsLink}
            >
              Set up Discover
            </Anchor>
          </Alert>
        )}
        {failed && (
          <Alert color="yellow">
            {feed.settingsError
              ? "Discover settings could not be loaded. Reload this page to retry."
              : "Weekly trending is temporarily unavailable. Retry with Refresh trending."}
          </Alert>
        )}
        {data?.service_status === "unavailable" && !expired && (
          <Text size="sm">
            TMDB is temporarily unavailable. The dated cached feed is retained.
          </Text>
        )}
        {data?.status === "empty" && !expired && (
          <Text>
            No titles in this weekly TMDB feed. Try another media filter or
            refresh.
          </Text>
        )}
        {data?.fetched_at && !expired && (
          <Text size="xs">
            {data.status === "cached" ||
            (data.expires_at && Date.parse(data.expires_at) <= clock)
              ? "Cached TMDB feed"
              : "Fetched from TMDB"}{" "}
            ·{" "}
            <time dateTime={data.fetched_at}>
              {new Date(data.fetched_at).toLocaleString()}
            </time>
          </Text>
        )}
      </div>
      {items.length > 0 && (
        <ul className={styles.posterGrid} aria-label="Weekly trending titles">
          {items.map((item) => (
            <li key={item.source_id}>
              <button
                id={`discover-trending-${item.source_id}`}
                className={styles.poster}
                type="button"
                onClick={() =>
                  open(item, `discover-trending-${item.source_id}`)
                }
              >
                <span className={styles.posterArt}>
                  <Artwork key={item.poster_url} src={item.poster_url} poster />
                </span>
                <strong>{item.title}</strong>
                <span>
                  {item.year ?? "Year unavailable"} ·{" "}
                  {item.media_type === "series" ? "Series" : "Film"}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {data?.last_success && (
        <details className={styles.feedDates}>
          <summary>Feed source and freshness</summary>
          <Text size="xs">
            TMDB global weekly ranking, first page. People are excluded. Library
            membership does not determine this ranking.
          </Text>
          <dl>
            {[
              ["Last successful fetch", data.last_success],
              ["Fresh until", data.expires_at],
              ["Cached fallback until", data.stale_until],
              ["Last request", data.attempted_at],
            ].map(
              ([label, value]) =>
                value && (
                  <div key={label}>
                    <dt>{label}</dt>
                    <dd>
                      <time dateTime={value}>
                        {new Date(value).toLocaleString()}
                      </time>
                    </dd>
                  </div>
                ),
            )}
          </dl>
        </details>
      )}
    </section>
  );
}
