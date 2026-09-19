import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router";
import { ActionIcon, Alert, Anchor, Button, Text, Title } from "@mantine/core";
import { useMediaQuery } from "@mantine/hooks";
import {
  faArrowsRotate,
  faChevronDown,
  faChevronRight,
  faChevronUp,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useDiscoverDigitalReleases } from "@/apis/hooks/discover";
import { useDiscover } from "@/contexts/Discover";
import type { DigitalRelease } from "@/types/discover";
import DiscoverSelect from "./DiscoverSelect";
import { readableFeedDate } from "./feedText";
import MediaPoster from "./MediaPoster";
import { discoverTitlePath } from "./navigation";
import styles from "./Discover.module.scss";

const regionNames = new Intl.DisplayNames(["en"], { type: "region" });
const regions =
  "AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW"
    .split(" ")
    .map((value) => ({
      value,
      label: `${regionNames.of(value)} (${value})`,
    }))
    .sort((left, right) => left.label.localeCompare(right.label));

export default function DigitalReleases() {
  const { state, updateBrowsing, updateDraft } = useDiscover();
  const navigate = useNavigate();
  const { browsing } = state;
  const region = browsing.digitalRegion;
  const feed = useDiscoverDigitalReleases(region);
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
  const items = useMemo(
    () => (expired ? [] : (data?.items ?? [])),
    [expired, data?.items],
  );
  const wide = useMediaQuery("(min-width: 1600px)");
  const medium = useMediaQuery("(min-width: 1100px)");
  const previewCount = wide ? 8 : medium ? 6 : 4;
  const returnedIndex = items.findIndex(
    (item) =>
      browsing.focusId === `discover-digital-${region}-${item.source_id}`,
  );
  const showAll = expanded || returnedIndex >= previewCount;
  const visibleItems = showAll ? items : items.slice(0, previewCount);
  const restored = useRef(false);
  useEffect(() => {
    if (restored.current || !browsing.focusId.startsWith("discover-digital-"))
      return;
    const opener = document.getElementById(browsing.focusId);
    if (!opener) return;
    const frame = window.requestAnimationFrame(() => {
      restored.current = true;
      opener.focus({ preventScroll: true });
      window.scrollTo({ top: browsing.scrollY, behavior: "instant" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [items, browsing.focusId, browsing.scrollY]);
  const open = (item: DigitalRelease) => {
    const reopen =
      browsing.identityLoaded && browsing.adoptedSourceId === item.source_id;
    updateBrowsing({
      selectedSource: "tmdb",
      selectedId: item.id,
      selectedType: "movie",
      selectedSeason: null,
      selectedEpisode: null,
      adoptedSourceId: reopen ? item.source_id : null,
      identityLoaded: reopen,
      adoptedMovieId: reopen ? item.id : null,
      focusId: `discover-digital-${region}-${item.source_id}`,
      scrollY: window.scrollY,
      returnTarget: location.pathname + location.search + location.hash,
      /* eslint-disable camelcase -- the release context is stored and sent with
         the source's own field names. */
      releaseContext: {
        source_id: item.source_id,
        release_date: item.release_date,
        release_type: "digital",
        region: item.region,
        fetched_at: data?.fetched_at ?? null,
        window: data!.window,
      },
      /* eslint-enable camelcase */
    });
    void navigate(discoverTitlePath("tmdb", "movie", item.id));
    if (!reopen)
      updateDraft({
        mode: "title",
        mediaType: "movie",
        imdbId: "",
        title: item.title,
        year: item.year ?? undefined,
        showId: undefined,
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
  return (
    <section
      id="bh-digital-section"
      className={`${styles.trending} ${styles.compactFeed}`}
      aria-labelledby="digital-title"
    >
      <div className={styles.sectionHead}>
        <div>
          <Title order={2} id="digital-title">
            Recent digital releases
          </Title>
          <Text component="p" className={styles.sectionMeta}>
            Digital · Last 30 days · {region}
          </Text>
        </div>
        <div className={styles.sectionTools}>
          <DiscoverSelect
            id="discover-digital-region"
            label="Film region"
            className={styles.regionSelect}
            searchable
            options={regions}
            value={region}
            onChange={(value) => {
              setExpanded(false);
              updateBrowsing({
                digitalRegion: value,
                focusId: "discover-digital-region",
              });
            }}
          />
          {feed.configured && (
            <ActionIcon
              id="discover-digital-refresh"
              variant="subtle"
              className={styles.refreshButton}
              aria-label="Refresh digital releases"
              title="Refresh digital releases"
              loading={feed.isFetching}
              onClick={() => void feed.refetch()}
            >
              <FontAwesomeIcon icon={faArrowsRotate} />
            </ActionIcon>
          )}
        </div>
      </div>

      <div role="status" aria-live="polite" className={styles.feedStatus}>
        {(feed.settingsLoading || feed.isFetching) && (
          <Text size="sm">Checking digital releases in {region}.</Text>
        )}
        {setup && (
          <Alert color="yellow">
            <Text size="sm">
              {data?.status === "authentication_failed"
                ? "TMDB rejected the key Discover is using."
                : "Connect TMDB to browse regional digital releases."}
            </Text>
            <Anchor
              component={Link}
              to="/subtitle-hub?tab=my-providers#metadata"
              className={styles.settingsLink}
            >
              Set up digital releases
            </Anchor>
          </Alert>
        )}
        {failed && (
          <Alert color="yellow">
            {feed.settingsError
              ? "Discover settings could not be loaded. Reload this page to retry."
              : "Digital releases are temporarily unavailable. Retry with Refresh digital releases."}
          </Alert>
        )}
        {data?.service_status === "unavailable" && !expired && (
          <Text size="sm">
            Some release checks are unavailable. Showing the releases found so
            far.
          </Text>
        )}

        {data?.status === "empty" && !expired && (
          <Text>No recent digital releases found in {region}.</Text>
        )}
      </div>
      {items.length > 0 && (
        <ul
          id="bh-digital-releases"
          className={styles.episodeList}
          aria-label="Recent digital films"
        >
          {visibleItems.map((item) => (
            <li key={item.source_id}>
              <button
                id={`discover-digital-${region}-${item.source_id}`}
                type="button"
                className={styles.episodeRow}
                onClick={() => open(item)}
              >
                <span className={styles.episodeArt}>
                  <MediaPoster
                    key={item.backdrop_url ?? item.poster_url}
                    src={item.backdrop_url ?? item.poster_url}
                  />
                </span>
                <span className={styles.episodeFacts}>
                  <strong>{item.title}</strong>
                  <span className={styles.episodeName}>
                    {item.year ? `${item.year} · Film` : "Film"}
                  </span>
                  <time
                    className={styles.dateValue}
                    dateTime={item.release_date}
                  >
                    {readableFeedDate(item.release_date)}
                  </time>
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
          aria-controls="bh-digital-releases"
          rightSection={
            <FontAwesomeIcon icon={showAll ? faChevronUp : faChevronDown} />
          }
          id="discover-digital-more"
          onClick={() => {
            setExpanded(!showAll);
            updateBrowsing({ focusId: "discover-digital-more" });
          }}
        >
          {showAll
            ? "Show fewer releases"
            : `Show all ${items.length} releases`}
        </Button>
      )}
    </section>
  );
}
