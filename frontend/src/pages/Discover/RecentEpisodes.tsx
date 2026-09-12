/* eslint-disable camelcase -- API context retains source field names. */
import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router";
import {
  ActionIcon,
  Alert,
  Anchor,
  Button,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { useMediaQuery } from "@mantine/hooks";
import {
  faArrowsRotate,
  faChevronDown,
  faChevronRight,
  faChevronUp,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useDiscoverRecentEpisodes } from "@/apis/hooks/discover";
import { useDiscover } from "@/contexts/Discover";
import type { RecentEpisode } from "@/types/discover";
import { plural, readableFeedDate } from "./feedText";
import MediaPoster from "./MediaPoster";
import { discoverTitlePath } from "./navigation";
import styles from "./Discover.module.scss";

export default function RecentEpisodes() {
  const { state, updateBrowsing, updateDraft } = useDiscover();
  const navigate = useNavigate();
  const { browsing } = state;
  const feed = useDiscoverRecentEpisodes();
  const data = feed.data;
  const location = useLocation();
  const [expanded, setExpanded] = useState(false);
  const [clock, setClock] = useState(Date.now);
  useEffect(() => {
    const timers = [data?.expires_at, data?.stale_until]
      .filter((value): value is string => Boolean(value))
      .map((value) =>
        window.setTimeout(
          () => setClock(Date.now()),
          Math.max(0, Date.parse(value) - Date.now()) + 1,
        ),
      );
    return () => timers.forEach(window.clearTimeout);
  }, [data?.expires_at, data?.stale_until]);
  const expired = Boolean(
    data?.stale_until && Date.parse(data.stale_until) <= clock,
  );
  const restored = useRef(false);
  useEffect(() => {
    if (restored.current || !browsing.focusId.startsWith("discover-recent-"))
      return;
    const opener = document.getElementById(browsing.focusId);
    if (!opener) return;
    const frame = window.requestAnimationFrame(() => {
      restored.current = true;
      opener.focus({ preventScroll: true });
      window.scrollTo({ top: browsing.scrollY, behavior: "instant" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [data?.items, browsing.focusId, browsing.scrollY]);
  const open = (item: RecentEpisode) => {
    updateBrowsing({
      selectedSource: "tmdb",
      selectedId: item.show_id,
      selectedType: "show",
      selectedSeason: item.season,
      selectedEpisode: item.episode,
      adoptedSourceId: null,
      adoptedMovieId: null,
      identityLoaded: false,
      focusId: `discover-recent-${item.source_id}`,
      scrollY: window.scrollY,
      returnTarget: location.pathname + location.search + location.hash,
      recentContext: {
        item,
        fetched_at: data!.fetched_at,
        window: data!.window,
      },
    });
    void navigate(
      discoverTitlePath(
        "tmdb",
        "show",
        item.show_id,
        item.season,
        item.episode,
      ),
    );
    updateDraft({
      mode: "title",
      mediaType: "episode",
      imdbId: "",
      title: item.show_title,
      year: item.show_year ?? undefined,
      showId: item.show_id,
      showTvdbId: undefined,
      episodeIdentity: undefined,
      manualConfirmed: false,
      manualEntry: false,
      season: "",
      episode: "",
    });
  };
  const setup =
    !feed.settingsLoading &&
    !feed.settingsError &&
    (!feed.configured ||
      data?.status === "unconfigured" ||
      data?.status === "authentication_failed");
  const failed =
    feed.settingsError ||
    feed.isError ||
    expired ||
    data?.status === "unavailable" ||
    (feed.isSuccess && !data);
  const items = expired || setup ? [] : (data?.items ?? []);
  const returnedIndex = items.findIndex(
    (item) => browsing.focusId === `discover-recent-${item.source_id}`,
  );
  const wide = useMediaQuery("(min-width: 1600px)");
  const medium = useMediaQuery("(min-width: 1100px)");
  const previewCount = wide ? 8 : medium ? 6 : 4;
  const showAll = expanded || returnedIndex >= previewCount;
  const visibleItems = showAll ? items : items.slice(0, previewCount);
  return (
    <section
      id="bh-episode-section"
      className={`${styles.trending} ${styles.compactFeed}`}
      aria-labelledby="recent-title"
    >
      <div className={styles.sectionHead}>
        <div>
          <Title order={2} id="recent-title">
            New episodes
          </Title>
        </div>
        {feed.configured && (
          <div className={styles.sectionTools}>
            <ActionIcon
              id="discover-recent-refresh"
              variant="subtle"
              className={styles.refreshButton}
              loading={feed.isFetching}
              onClick={() => void feed.refetch()}
              aria-label="Refresh new episodes"
              title="Refresh new episodes"
            >
              <FontAwesomeIcon icon={faArrowsRotate} />
            </ActionIcon>
          </div>
        )}
      </div>
      <Stack
        role="status"
        aria-live="polite"
        gap="xs"
        className={styles.feedStatus}
      >
        {(feed.settingsLoading || feed.isFetching) && (
          <Text size="sm">Checking recent episodes.</Text>
        )}
        {setup && (
          <Alert color="yellow">
            <Text size="sm">
              {data?.status === "authentication_failed"
                ? "TMDB rejected the key Discover is using."
                : "Connect TMDB to browse recent episodes."}
            </Text>
            <Anchor
              component={Link}
              to="/subtitle-hub?tab=my-providers#metadata"
              className={styles.settingsLink}
            >
              Set up recent episodes
            </Anchor>
          </Alert>
        )}
        {failed && (
          <Alert color="yellow">
            {feed.settingsError
              ? "Discover settings could not be loaded. Reload this page to retry."
              : "New episodes are temporarily unavailable. Retry with Refresh new episodes."}
          </Alert>
        )}
        {data?.service_status === "unavailable" && !expired && (
          <Text size="sm">
            Some source checks are unavailable. Usable cached records keep their
            original observation time.
          </Text>
        )}
        {data?.status === "empty" && !expired && (
          <Text>
            No qualifying episodes were found in the checked records. Unchecked
            seasons may contain other releases.
          </Text>
        )}
      </Stack>
      {items.length > 0 && (
        <ul
          id="bh-new-episodes"
          className={styles.episodeList}
          aria-label="New episodes"
        >
          {visibleItems.map((item) => (
            <li key={item.source_id}>
              <button
                type="button"
                id={`discover-recent-${item.source_id}`}
                className={styles.episodeRow}
                onClick={() => open(item)}
              >
                <span className={styles.episodeArt}>
                  <MediaPoster key={item.poster_url} src={item.poster_url} />
                </span>
                <span className={styles.episodeFacts}>
                  <strong>{item.show_title}</strong>
                  <span className={styles.episodeName}>
                    <span className={styles.episodeCode}>
                      {item.season === 0 ? "Special" : `S${item.season}`} E
                      {item.episode}
                    </span>
                    {" · "}
                    {item.title}
                  </span>
                  {item.identity_status === "conflict" && (
                    <span className={styles.episodeConflict}>
                      Source numbering conflict. Review episode details.
                    </span>
                  )}
                  <span>
                    <time
                      className={styles.dateValue}
                      dateTime={item.air_date ?? undefined}
                    >
                      {item.air_date === null
                        ? "release date to be announced"
                        : readableFeedDate(item.air_date)}
                    </time>
                  </span>
                </span>
                <FontAwesomeIcon
                  icon={faChevronRight}
                  className={styles.episodeChevron}
                  aria-hidden="true"
                />
              </button>
            </li>
          ))}
        </ul>
      )}
      {items.length > previewCount && (
        <Button
          variant="default"
          className={styles.showMore}
          aria-expanded={showAll}
          aria-controls="bh-new-episodes"
          rightSection={
            <FontAwesomeIcon icon={showAll ? faChevronUp : faChevronDown} />
          }
          id="discover-recent-more"
          onClick={() => {
            setExpanded(!showAll);
            updateBrowsing({ focusId: "discover-recent-more" });
          }}
        >
          {showAll
            ? "Show fewer episodes"
            : `Show all ${items.length} episodes`}
        </Button>
      )}
    </section>
  );
}
