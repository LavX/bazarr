import { useEffect, useMemo, useRef } from "react";
import { useNavigate } from "react-router";
import { Alert, Button, Stack, Text } from "@mantine/core";
import { useDiscoverMetadata } from "@/apis/hooks/discover";
import { useDiscover } from "@/contexts/Discover";
import {
  episodeIdentityKey,
  episodeMatchesShow,
} from "@/contexts/discoverState";
import type { MetadataShow } from "@/types/discover";
import DiscoverSelect from "./DiscoverSelect";
import styles from "./Discover.module.scss";

export function episodeLink(showId: number, season: number, episode: number) {
  return `/discover?show=${showId}&season=${season}&episode=${episode}`;
}

export default function EpisodePicker({
  show,
  showOptions = false,
}: {
  show: MetadataShow;
  showOptions?: boolean;
}) {
  const { state, updateBrowsing, updateDraft } = useDiscover();
  const { browsing, draft } = state;
  const season = browsing.selectedSeason;
  const number = browsing.selectedEpisode;
  const navigate = useNavigate();
  const seasons = useDiscoverMetadata(
    `shows/${show.id}/seasons/${season}`,
    undefined,
    season !== null,
  );
  const details = useDiscoverMetadata(
    `shows/${show.id}/seasons/${season}/episodes/${number}`,
    undefined,
    season !== null && number !== null,
  );
  // The same query the detail page already holds. The guard compares two
  // fetched objects, so whichever one is wrong has to be refetchable from
  // here: refetching only the episode repeats a request whose answer was never
  // the disagreeing side.
  const showDetails = useDiscoverMetadata(
    `shows/${show.id}`,
    undefined,
    true,
    "show",
    "tmdb",
  );
  const refetchEpisode = details.refetch;
  const refetchShow = showDetails.refetch;
  const received = details.data?.episode;
  const episode =
    received ??
    (draft.episodeIdentity?.show_id === show.id &&
    draft.episodeIdentity.season === season &&
    draft.episodeIdentity.episode === number
      ? draft.episodeIdentity
      : undefined);
  const currentDraft = useRef(draft);
  currentDraft.current = draft;
  const parentMatches = episode ? episodeMatchesShow(episode, show) : true;
  useEffect(() => {
    if (
      !received ||
      !episode ||
      episode.show_id !== show.id ||
      episode.season !== season ||
      episode.episode !== number
    )
      return;
    if (
      episodeIdentityKey(currentDraft.current.episodeIdentity) ===
      episodeIdentityKey(episode)
    )
      return;
    // Adoption is deliberately not gated on the episode agreeing with the show
    // item. The three conditions above already pin it to this link's own show,
    // season and number, so it is the source's current answer for the episode
    // the reader asked for, and refusing while a disagreement lasts is what
    // made the warning unrecoverable.
    updateDraft({
      mediaType: "episode",
      showId: episode.show_id,
      // The episode carries its own view of the show's identity, which is the
      // view a search is gated on. Writing a disagreeing one over the show
      // item's would leave the request carrying the mapping the warning above
      // is about, so the show item's copy stands until the two agree.
      ...(parentMatches
        ? {
            showTvdbId: episode.show_tvdb_id,
            imdbId: episode.show_imdb_id ?? "",
            title: episode.show_title,
            year: episode.show_year ?? undefined,
          }
        : {}),
      episodeIdentity: episode,
      season: episode.target_season?.toString() ?? "",
      episode: episode.target_episode?.toString() ?? "",
      manualConfirmed: false,
      manualEntry: false,
    });
  }, [received, episode, parentMatches, show.id, season, number, updateDraft]);
  const reconciliation = useRef<string | null>(null);
  const parentKey = JSON.stringify([
    show.id,
    show.imdb_id,
    show.tvdb_id,
    show.title,
    show.year,
    season,
    number,
  ]);
  useEffect(() => {
    if (!parentMatches && reconciliation.current !== parentKey) {
      reconciliation.current = parentKey;
      void refetchEpisode();
      void refetchShow();
    }
  }, [parentMatches, parentKey, refetchEpisode, refetchShow]);
  const unavailable = seasons.isError || seasons.data?.status === "unavailable";
  const conflicting = episode?.identity_status === "conflict";
  const unverified = episode?.identity_status === "unverified";
  const needsManual =
    show.seasons === null ||
    show.seasons.length === 0 ||
    unavailable ||
    unverified;
  // The link's own numbering is the only numbering the page knows here, so the
  // reader confirms or corrects it rather than retyping what they just
  // followed. An edit still wins: these are ordinary draft fields.
  const beginManual = () =>
    updateDraft({
      manualEntry: true,
      manualConfirmed: false,
      season: season?.toString() ?? "",
      episode: number?.toString() ?? "",
    });
  const seasonRows = useMemo(() => show.seasons ?? [], [show.seasons]);
  const episodeRows =
    seasons.data?.season?.season === season ? seasons.data.season.episodes : [];
  // A single season has no meaningful choice. Keep that default in the URL
  // without adding a second history stop when the show first opens.
  useEffect(() => {
    if (season !== null || seasonRows.length !== 1) return;
    const onlySeason = seasonRows[0].season;
    void navigate(`/discover?show=${show.id}&season=${onlySeason}`, {
      replace: true,
    });
  }, [season, seasonRows, show.id, navigate]);

  return (
    <Stack className={styles.episodePicker} gap="sm">
      <div className={styles.episodeSelectors}>
        <DiscoverSelect
          label="Season"
          placeholder={
            seasonRows.length ? "Choose a season" : "No seasons available"
          }
          disabled={!seasonRows.length}
          value={season?.toString() ?? ""}
          options={seasonRows.map((row) => ({
            value: String(row.season),
            label: row.title,
          }))}
          onChange={(value) => {
            const next = value === "" ? null : Number(value);
            updateBrowsing({ selectedSeason: next, selectedEpisode: null });
            updateDraft({
              episodeIdentity: undefined,
              season: "",
              episode: "",
              manualConfirmed: false,
              manualEntry: false,
            });
            void navigate(
              `/discover?show=${show.id}${next === null ? "" : `&season=${next}`}`,
            );
          }}
        />
        <DiscoverSelect
          label="Episode"
          placeholder={
            season === null
              ? "Choose a season first"
              : seasons.isFetching
                ? "Loading episodes…"
                : "Choose an episode"
          }
          disabled={season === null || !episodeRows.length}
          value={number?.toString() ?? ""}
          options={episodeRows.map((row) => ({
            value: String(row.episode),
            label: `${row.episode}. ${row.title}`,
          }))}
          onChange={(value) => {
            if (season !== null && value)
              void navigate(episodeLink(show.id, season, Number(value)));
          }}
        />
      </div>
      {show.seasons === null && (
        <Text size="sm" c="dimmed">
          Season details are unavailable for this show.
        </Text>
      )}
      {show.seasons?.length === 0 && (
        <Text size="sm" c="dimmed">
          No episodes have been listed for this show yet.
        </Text>
      )}
      {season !== null && unavailable && (
        <Alert color="yellow">
          Episodes could not be loaded.
          <Button
            type="button"
            variant="subtle"
            onClick={() => void seasons.refetch()}
          >
            Retry
          </Button>
        </Alert>
      )}
      {season !== null &&
        !seasons.isFetching &&
        !unavailable &&
        seasons.data?.season &&
        !episodeRows.length && (
          <Text size="sm" c="dimmed">
            No episodes have been listed for this season yet.
          </Text>
        )}
      {number !== null && details.isFetching && (
        <Text size="sm" role="status">
          Loading episode details…
        </Text>
      )}
      {number !== null &&
        (details.isError ||
          details.data?.status === "unavailable" ||
          details.data?.service_status === "unavailable") && (
          <Alert color="yellow">
            Episode details are temporarily unavailable. Your selection is
            retained.
            <Button
              type="button"
              variant="subtle"
              onClick={() => void details.refetch()}
            >
              Retry
            </Button>
          </Alert>
        )}
      {episode && !parentMatches && !unverified && (
        <Alert color="yellow">
          Show details changed. Refresh the episode and the show before
          searching.
          <Button
            type="button"
            variant="subtle"
            onClick={() => {
              void refetchEpisode();
              void refetchShow();
            }}
          >
            Refresh details
          </Button>
        </Alert>
      )}
      {/* Nothing changed here and nothing can be refreshed into place: the
          metadata source carries no TVDB mapping for this title, so its season
          and episode numbers cannot be computed. Said in the title's own terms
          rather than as a fault, and with the manual path next to it, because
          confirming the numbering by hand is the only route to a search. */}
      {unverified && !draft.manualEntry && (
        <Text size="sm">
          This title has no TVDB series id at the metadata source, so its season
          and episode numbers cannot be verified automatically. Confirm this
          episode's season and number to search.
        </Text>
      )}
      {conflicting && (
        <Alert color="red">
          Episode numbering does not match between metadata sources. Refresh
          details before searching.
          <Button
            type="button"
            variant="subtle"
            onClick={() => void details.refetch()}
          >
            Refresh episode
          </Button>
        </Alert>
      )}
      {show.imdb_id &&
        !conflicting &&
        !draft.manualEntry &&
        (showOptions || needsManual || !parentMatches) && (
          <Button
            type="button"
            variant="subtle"
            className={styles.episodeManual}
            onClick={beginManual}
          >
            Enter episode numbers manually
          </Button>
        )}
      {draft.manualEntry && (
        <Text size="sm">Confirm the season and episode numbers below.</Text>
      )}
    </Stack>
  );
}
