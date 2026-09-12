import {
  CSSProperties,
  MouseEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Link, useLocation, useNavigate } from "react-router";
import {
  ActionIcon,
  Alert,
  Anchor,
  Button,
  Text,
  Title,
  VisuallyHidden,
} from "@mantine/core";
import { useMediaQuery } from "@mantine/hooks";
import {
  faArrowLeft,
  faArrowRight,
  faArrowsRotate,
  faChevronDown,
  faChevronUp,
  faFilm,
  faFireFlameCurved,
  faGlobe,
  faLayerGroup,
  faTv,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useDiscoverTrending } from "@/apis/hooks/discover";
import { useDiscover } from "@/contexts/Discover";
import type { TrendingMediaType, TrendingTitle } from "@/types/discover";
import MediaPoster, { Backdrop } from "./MediaPoster";
import { discoverTitlePath } from "./navigation";
import styles from "./Discover.module.scss";

const filters: { value: TrendingMediaType; label: string }[] = [
  { value: "all", label: "All media" },
  { value: "movie", label: "Movies" },
  { value: "series", label: "Series" },
];

export default function Trending() {
  const { state, updateBrowsing, updateDraft } = useDiscover();
  const navigate = useNavigate();
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
  // The spotlight cycles through the live feed, starting with the top rank.
  // Featured-title navigation never touches either catalog filter.
  const featured =
    items.find((item) => item.source_id === browsing.featuredSourceId) ??
    items[0];
  const [expanded, setExpanded] = useState(false);
  const posterItems = items;
  const returnedIndex = posterItems.findIndex(
    (item) => browsing.focusId === `discover-trending-${item.source_id}`,
  );
  const wide = useMediaQuery("(min-width: 1600px)");
  const medium = useMediaQuery("(min-width: 1200px)");
  const previewCount = wide ? 8 : medium ? 6 : 4;
  const showAll = expanded || returnedIndex >= previewCount;
  const previewItems = posterItems.slice(0, previewCount);
  // Keep the hero's card visible even when cycling beyond the preview row.
  const visibleItems = showAll
    ? posterItems
    : featured &&
        !previewItems.some((item) => item.source_id === featured.source_id)
      ? [...previewItems.slice(0, previewCount - 1), featured]
      : previewItems;
  const changeFeatured = (event: MouseEvent, direction: number) => {
    event.preventDefault();
    if (items.length > 1)
      updateBrowsing({
        featuredSourceId:
          items[
            (items.indexOf(featured) + direction + items.length) % items.length
          ].source_id,
      });
  };
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
    void navigate(
      discoverTitlePath(
        "tmdb",
        kind,
        item.id,
        reopen ? browsing.selectedSeason : null,
        reopen ? browsing.selectedEpisode : null,
      ),
    );
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
                data-filter={filter.value}
                aria-pressed={browsing.trendingFilter === filter.value}
                onClick={() =>
                  // Filters apply to this feed. Global search keeps all media types.
                  updateBrowsing({
                    trendingFilter: filter.value,
                    featuredSourceId: null,
                    ...(filter.value === "movie"
                      ? { mediaFilter: "movie" as const }
                      : filter.value === "series"
                        ? { mediaFilter: "show" as const }
                        : {}),
                    focusId: `discover-trending-filter-${filter.value}`,
                  })
                }
                id={`discover-trending-filter-${filter.value}`}
              >
                <FontAwesomeIcon
                  icon={
                    filter.value === "movie"
                      ? faFilm
                      : filter.value === "series"
                        ? faTv
                        : faLayerGroup
                  }
                  aria-hidden="true"
                />
                {filter.label}
              </button>
            ))}
          </div>
          <Text component="p" className={styles.globalLabel}>
            <FontAwesomeIcon icon={faGlobe} aria-hidden="true" />
            Global catalog
          </Text>
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
              {items.length > 1 && (
                <div
                  className={styles.featureNavigation}
                  role="group"
                  aria-label="Featured title navigation"
                >
                  <button
                    type="button"
                    className={styles.featureNext}
                    aria-label="Show previous featured title"
                    onClick={(event) => changeFeatured(event, -1)}
                  >
                    <FontAwesomeIcon icon={faArrowLeft} aria-hidden="true" />
                  </button>
                  <button
                    type="button"
                    className={styles.featureNext}
                    aria-label="Show next featured title"
                    onClick={(event) => changeFeatured(event, 1)}
                  >
                    <FontAwesomeIcon icon={faArrowRight} aria-hidden="true" />
                  </button>
                </div>
              )}
              <div className={styles.featureCopy}>
                <Title order={2} id="bh-feature-title">
                  {featured.title}
                </Title>
                <Text className={styles.featureMeta}>
                  No. {featured.rank} this week ·{" "}
                  {featured.year ?? "Year unavailable"} ·{" "}
                  {featured.media_type === "series" ? "Series" : "Film"}
                </Text>
                <Button
                  variant="filled"
                  className={styles.featureCta}
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
      <section
        aria-labelledby="bh-trending-label"
        className={`${styles.trending} ${styles.weeklyTrending}`}
      >
        <div className={styles.sectionHead}>
          <div>
            <Title order={2} id="bh-trending-label">
              <FontAwesomeIcon icon={faFireFlameCurved} aria-hidden="true" />{" "}
              Trending this week
            </Title>
          </div>
          <div className={styles.sectionTools}>
            {feed.configured && (
              <ActionIcon
                variant="subtle"
                className={styles.refreshButton}
                loading={feed.isFetching}
                onClick={() => void feed.refetch()}
                aria-label="Refresh trending"
                title="Refresh trending"
              >
                <FontAwesomeIcon icon={faArrowsRotate} />
              </ActionIcon>
            )}
          </div>
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
                to="/subtitle-hub?tab=my-providers#metadata"
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
              TMDB is temporarily unavailable. Showing saved titles.
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
          <ul
            id="bh-posters"
            className={styles.posterGrid}
            aria-label="Weekly trending titles"
          >
            {visibleItems.map((item) => (
              <li key={item.source_id}>
                <button
                  id={`discover-trending-${item.source_id}`}
                  className={styles.poster}
                  data-featured={item.source_id === featured?.source_id}
                  aria-current={
                    item.source_id === featured?.source_id ? "true" : undefined
                  }
                  type="button"
                  onClick={() =>
                    open(item, `discover-trending-${item.source_id}`)
                  }
                >
                  <span className={styles.posterArt}>
                    <MediaPoster key={item.poster_url} src={item.poster_url} />
                    {item.source_id === featured?.source_id && (
                      <span className={styles.featuredBadge}>Featured</span>
                    )}
                    <span className={styles.weeklyRank} aria-hidden="true">
                      {String(item.rank).padStart(2, "0")}
                    </span>
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
        {posterItems.length > previewCount && (
          <Button
            variant="default"
            className={styles.showMore}
            aria-expanded={showAll}
            aria-controls="bh-posters"
            rightSection={
              <FontAwesomeIcon icon={showAll ? faChevronUp : faChevronDown} />
            }
            onClick={() => {
              setExpanded(!showAll);
              updateBrowsing({ focusId: "discover-trending-more" });
            }}
            id="discover-trending-more"
          >
            {showAll
              ? "Show fewer titles"
              : `Show all ${posterItems.length} titles`}
          </Button>
        )}
      </section>
    </>
  );
}
