/* eslint-disable camelcase -- API context retains source field names. */
import { useEffect, useRef, useState } from "react";
import { Link, useLocation } from "react-router";
import { Alert, Anchor, Button, Stack, Text, Title } from "@mantine/core";
import { faArrowsRotate } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useDiscoverRecentEpisodes } from "@/apis/hooks/discover";
import { useDiscover } from "@/contexts/Discover";
import type { RecentEpisode } from "@/types/discover";
import { plural, readableTime } from "./feedText";
import MediaPoster from "./MediaPoster";
import styles from "./Discover.module.scss";

export default function RecentEpisodes() {
  const { state, updateBrowsing, updateDraft } = useDiscover();
  const { browsing } = state;
  const feed = useDiscoverRecentEpisodes();
  const data = feed.data;
  const location = useLocation();
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
  return (
    <section className={styles.trending} aria-labelledby="recent-title">
      <div className={styles.sectionHead}>
        <div>
          <Title order={2} id="recent-title">
            Recent episodes
          </Title>
          <Text component="p" className={styles.sectionMeta}>
            Original air dates · Last 30 days
          </Text>
        </div>
        {feed.configured && (
          <div className={styles.sectionTools}>
            <Button
              id="discover-recent-refresh"
              variant="subtle"
              loading={feed.isFetching}
              leftSection={<FontAwesomeIcon icon={faArrowsRotate} />}
              onClick={() => void feed.refetch()}
            >
              Refresh recent episodes
            </Button>
          </div>
        )}
      </div>
      <Text component="p" className={styles.caveat}>
        A selection from weekly trending shows, not a complete schedule or a
        ranking of episode popularity. Subtitle availability and exact episode
        ownership are unchecked. Verify episode identity in details.
      </Text>
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
              to="/settings/discover"
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
              : "Recent episodes are temporarily unavailable. Retry with Refresh recent episodes."}
          </Alert>
        )}
        {data?.service_status === "unavailable" && !expired && (
          <Text size="sm">
            Some source checks are unavailable. Usable cached records keep their
            original observation time.
          </Text>
        )}
        {data && !data.coverage.complete && !expired && !setup && (
          <Text size="sm">
            Incomplete episode coverage. The counts are in Episode source and
            freshness below.
          </Text>
        )}
        {data?.status === "empty" && !expired && (
          <Text>
            No qualifying episodes were found in the checked records. Unchecked
            seasons may contain other releases.
          </Text>
        )}
        {data?.fetched_at && !expired && (
          <Text component="p" className={styles.stamp}>
            {data.status === "cached" ||
            (data.expires_at && Date.parse(data.expires_at) <= clock)
              ? "Cached TMDB episode records"
              : "Checked TMDB episode records"}{" "}
            ·{" "}
            <time className={styles.dateValue} dateTime={data.fetched_at}>
              {readableTime(data.fetched_at)}
            </time>
          </Text>
        )}
      </Stack>
      {items.length > 0 && (
        <ul className={styles.episodeList} aria-label="Recent episodes">
          {items.map((item) => (
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
                  <span>
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
                    Original air date{" "}
                    <time className={styles.dateValue} dateTime={item.air_date}>
                      {item.air_date}
                    </time>
                  </span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {data?.last_success && (
        <details className={styles.feedDates}>
          <summary>Episode source and freshness</summary>
          <Text size="xs">
            TMDB weekly trending shows and original season records. Up to{" "}
            {data.coverage.show_limit} shows, {data.coverage.season_limit}{" "}
            seasons per show including specials, and{" "}
            {data.coverage.episode_limit} records per season. Up to{" "}
            {data.coverage.output_limit} qualifying episodes. Dates run from{" "}
            {data.window.start} through {data.window.end}, inclusive, using UTC
            today.
          </Text>
          {!data.coverage.complete && (
            <Text size="xs">
              Checked {data.coverage.shows_checked} of{" "}
              {plural(data.coverage.shows, "show")} and{" "}
              {data.coverage.seasons_checked} of{" "}
              {plural(data.coverage.seasons, "season")}.{" "}
              {plural(data.coverage.failed, "check")} failed, and{" "}
              {plural(data.coverage.missing_dates, "date")}{" "}
              {data.coverage.missing_dates === 1 ? "was" : "were"} missing or
              invalid.
              {data.coverage.truncated
                ? " Additional source records are outside this bounded selection."
                : ""}
            </Text>
          )}
          <dl>
            {[
              ["Oldest source observation", data.last_success],
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
