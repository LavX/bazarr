import { CSSProperties, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation } from "react-router";
import {
  Alert,
  Anchor,
  Button,
  Text,
  Title,
  VisuallyHidden,
} from "@mantine/core";
import {
  faArrowRight,
  faArrowsRotate,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useDiscoverTrending } from "@/apis/hooks/discover";
import { useDiscover } from "@/contexts/Discover";
import type { TrendingMediaType, TrendingTitle } from "@/types/discover";
import { readableTime } from "./feedText";
import MediaPoster, { Backdrop } from "./MediaPoster";
import styles from "./Discover.module.scss";

const filters: { value: TrendingMediaType; label: string }[] = [
  { value: "all", label: "All media" },
  { value: "movie", label: "Movies" },
  { value: "series", label: "Series" },
];

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
    <>
      <section aria-label="Spotlight" className={styles.spotlight}>
        <div className={styles.filterRow}>
          <div
            role="group"
            aria-label="Global trending media"
            className={styles.filterTabs}
          >
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
          <Text component="p">Global catalog · Weekly</Text>
        </div>
        {featured && (
          <div
            className={styles.stage}
            data-artwork={featured.backdrop_url ? "true" : "false"}
            style={
              featured.backdrop_url
                ? ({
                    "--feature-backdrop": `url("${featured.backdrop_url}")`,
                  } as CSSProperties)
                : undefined
            }
          >
            <article
              className={styles.feature}
              // Without a backdrop there is nothing for a scrim to sit over, and
              // in day a near-black slab under the search field reads as
              // something that failed to load. The title block carries the
              // composition instead, on the page's own surface.
              data-artwork={featured.backdrop_url ? "true" : "false"}
            >
              <Backdrop
                key={featured.backdrop_url}
                src={featured.backdrop_url}
              />
              <div className={styles.featureCopy}>
                <Title order={2}>{featured.title}</Title>
                <Text className={styles.featureMeta}>
                  No. {featured.rank} this week ·{" "}
                  {featured.year ?? "Year unavailable"} ·{" "}
                  {featured.media_type === "series" ? "Series" : "Film"}
                </Text>
                {featured.overview && (
                  <Text className={styles.featureOverview} lineClamp={2}>
                    {featured.overview}
                  </Text>
                )}
                <Button
                  variant="filled"
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
          </div>
        )}
      </section>
      <section aria-labelledby="trending-title" className={styles.trending}>
        <div className={styles.sectionHead}>
          <div>
            <Title order={2} id="trending-title">
              Trending this week
            </Title>
            {data?.fetched_at && !expired && (
              <Text component="p" className={styles.sectionMeta}>
                {data.status === "cached" ||
                (data.expires_at && Date.parse(data.expires_at) <= clock)
                  ? "Cached TMDB feed"
                  : "Fetched from TMDB"}{" "}
                ·{" "}
                <time className={styles.dateValue} dateTime={data.fetched_at}>
                  {readableTime(data.fetched_at)}
                </time>
              </Text>
            )}
          </div>
          {feed.configured && (
            <div className={styles.sectionTools}>
              <Button
                variant="subtle"
                loading={feed.isFetching}
                leftSection={<FontAwesomeIcon icon={faArrowsRotate} />}
                onClick={() => void feed.refetch()}
              >
                Refresh trending
              </Button>
            </div>
          )}
        </div>
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
                  ? "TMDB rejected the key Discover is using."
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
              TMDB is temporarily unavailable. The dated cached feed is
              retained.
            </Text>
          )}
          {data?.status === "empty" && !expired && (
            <Text>
              No titles in this weekly TMDB feed. Try another media filter or
              refresh.
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
                    <span className={styles.rank} aria-hidden="true">
                      {item.rank}
                    </span>
                    <MediaPoster key={item.poster_url} src={item.poster_url} />
                  </span>
                  <strong>{item.title}</strong>
                  <span className={styles.captionValues}>
                    <span>{item.year ?? "Year unavailable"}</span>
                    <span>
                      {item.media_type === "series" ? "Series" : "Film"}
                    </span>
                  </span>
                  <VisuallyHidden>Trending number {item.rank}.</VisuallyHidden>
                </button>
              </li>
            ))}
          </ul>
        )}
        {data?.last_success && (
          <details className={styles.feedDates}>
            <summary>Feed source and freshness</summary>
            <Text size="xs">
              TMDB global weekly ranking, first page. People are excluded.
              Library membership does not determine this ranking.
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
    </>
  );
}
