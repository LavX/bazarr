import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation } from "react-router";
import {
  Alert,
  Anchor,
  Button,
  Group,
  NativeSelect,
  Text,
  Title,
} from "@mantine/core";
import { faFilm } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useDiscoverDigitalReleases } from "@/apis/hooks/discover";
import { useDiscover } from "@/contexts/Discover";
import type { DigitalRelease } from "@/types/discover";
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

function Poster({ src }: { src: string | null }) {
  const [failed, setFailed] = useState(false);
  return src && !failed ? (
    <img
      src={src}
      alt=""
      loading="lazy"
      decoding="async"
      onError={() => setFailed(true)}
    />
  ) : (
    <span className={styles.missingArt}>
      <FontAwesomeIcon icon={faFilm} />
      <span>Artwork unavailable</span>
    </span>
  );
}

export default function DigitalReleases() {
  const { state, updateBrowsing, updateDraft } = useDiscover();
  const { browsing } = state;
  const region = browsing.digitalRegion;
  const feed = useDiscoverDigitalReleases(region);
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
  const items = useMemo(
    () => (expired ? [] : (data?.items ?? [])),
    [expired, data?.items],
  );
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
      releaseContext: {
        source_id: item.source_id,
        release_date: item.release_date,
        release_type: "digital",
        region: item.region,
        fetched_at: data?.fetched_at ?? null,
        window: data!.window,
      },
    });
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
    <section className={styles.trending} aria-labelledby="digital-title">
      <Group className={styles.railHeading} justify="space-between">
        <Title order={2} id="digital-title">
          Recent digital releases
        </Title>
        {feed.configured && (
          <Button
            id="discover-digital-refresh"
            variant="subtle"
            loading={feed.isFetching}
            onClick={() => void feed.refetch()}
          >
            Refresh digital releases
          </Button>
        )}
      </Group>
      <Group align="end" mb="md">
        <NativeSelect
          id="discover-digital-region"
          label="Film region"
          w={280}
          maw="100%"
          data={regions}
          value={region}
          onChange={(event) =>
            updateBrowsing({
              digitalRegion: event.currentTarget.value,
              focusId: "discover-digital-region",
            })
          }
        />
        <Text size="sm">Digital · Last 30 days · {region}</Text>
      </Group>
      <Text size="sm" mb="md">
        Release availability does not confirm subtitle availability or
        synchronization.
      </Text>
      <div role="status" aria-live="polite" className={styles.feedStatus}>
        {(feed.settingsLoading || feed.isFetching) && (
          <Text size="sm">Checking digital releases in {region}.</Text>
        )}
        {setup && (
          <Alert color="yellow">
            <Text size="sm">
              {data?.status === "authentication_failed"
                ? "TMDB rejected the saved access token."
                : "Connect TMDB to browse regional digital releases."}
            </Text>
            <Anchor
              component={Link}
              to="/settings/discover"
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
            Some release checks are unavailable. Verified records keep their
            original check time.
          </Text>
        )}
        {data && !data.coverage.complete && !expired && !setup && (
          <Text size="sm">
            Incomplete regional coverage: {data.coverage.checked} of{" "}
            {data.coverage.candidates} candidates checked,{" "}
            {data.coverage.missing_region} missing {region} records,{" "}
            {data.coverage.failed} failed checks.
            {data.coverage.truncated
              ? " Coverage beyond this candidate page is not verified."
              : ""}
          </Text>
        )}
        {data?.status === "empty" && !expired && (
          <Text>
            No verified digital releases in {region} for this window. Another
            region's dates are never substituted.
          </Text>
        )}
        {data?.fetched_at && !expired && (
          <Text size="xs">
            {data.status === "cached" ||
            (data.expires_at && Date.parse(data.expires_at) <= clock)
              ? "Cached TMDB regional records"
              : "Checked TMDB regional records"}{" "}
            ·{" "}
            <time dateTime={data.fetched_at}>
              {new Date(data.fetched_at).toLocaleString()}
            </time>
          </Text>
        )}
      </div>
      {items.length > 0 && (
        <ul className={styles.posterGrid} aria-label="Recent digital films">
          {items.map((item) => (
            <li key={item.source_id}>
              <button
                id={`discover-digital-${region}-${item.source_id}`}
                type="button"
                className={styles.poster}
                onClick={() => open(item)}
              >
                <span className={styles.posterArt}>
                  <Poster key={item.poster_url} src={item.poster_url} />
                </span>
                <strong>{item.title}</strong>
                <span>
                  <time dateTime={item.release_date}>{item.release_date}</time>{" "}
                  · Digital · {item.region}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {data?.last_success && (
        <details className={styles.feedDates}>
          <summary>Digital release source and freshness</summary>
          <Text size="xs">
            TMDB regional release records, digital type 4. The first{" "}
            {data.coverage.candidate_limit} candidates from one discovery page
            are checked. Dates run from {data.window.start} through{" "}
            {data.window.end}, inclusive, using UTC today. Source calendar dates
            are never shifted by timezone.
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
