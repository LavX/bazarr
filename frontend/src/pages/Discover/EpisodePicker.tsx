import { useEffect, useRef } from "react";
import { Link, useLocation, useNavigate } from "react-router";
import {
  Alert,
  Anchor,
  Button,
  Group,
  NativeSelect,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { useDiscoverMetadata } from "@/apis/hooks/discover";
import { useDiscover } from "@/contexts/Discover";
import {
  episodeIdentityKey,
  episodeMatchesShow,
} from "@/contexts/discoverState";
import type { MetadataShow } from "@/types/discover";
import styles from "./Discover.module.scss";

export function episodeLink(showId: number, season: number, episode: number) {
  return `/discover?show=${showId}&season=${season}&episode=${episode}`;
}

export default function EpisodePicker({ show }: { show: MetadataShow }) {
  const { state, updateBrowsing, updateDraft } = useDiscover();
  const { browsing, draft } = state;
  const season = browsing.selectedSeason;
  const number = browsing.selectedEpisode;
  const navigate = useNavigate();
  const location = useLocation();
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
  const refetchEpisode = details.refetch;
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
      !parentMatches ||
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
    updateDraft({
      mediaType: "episode",
      showId: episode.show_id,
      showTvdbId: episode.show_tvdb_id,
      imdbId: episode.show_imdb_id ?? "",
      title: episode.show_title,
      year: episode.show_year ?? undefined,
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
    }
  }, [parentMatches, parentKey, refetchEpisode]);
  const unavailable = seasons.isError || seasons.data?.status === "unavailable";
  const conflicting = draft.episodeIdentity?.identity_status === "conflict";
  const beginManual = () =>
    updateDraft({
      manualEntry: true,
      manualConfirmed: false,
      season: "",
      episode: "",
    });
  return (
    <Stack className={styles.episodePicker} gap="md">
      <Title order={3}>Choose an episode</Title>
      <Text size="sm">
        Select a season, then an episode. Browsing does not search subtitle
        providers.
      </Text>
      {show.seasons === null ? (
        <Alert color="yellow">
          Season metadata is unavailable for this show.
        </Alert>
      ) : show.seasons.length === 0 ? (
        <Text>No seasons have been listed for this show.</Text>
      ) : (
        <NativeSelect
          label="Choose season"
          value={season?.toString() ?? ""}
          data={[
            { value: "", label: "Choose a season" },
            ...show.seasons.map((row) => ({
              value: String(row.season),
              label: row.title,
            })),
          ]}
          onChange={(event) => {
            const next =
              event.currentTarget.value === ""
                ? null
                : Number(event.currentTarget.value);
            updateBrowsing({ selectedSeason: next, selectedEpisode: null });
            updateDraft({
              episodeIdentity: undefined,
              season: "",
              episode: "",
              manualConfirmed: false,
              manualEntry: false,
            });
            if (location.search.includes("show="))
              void navigate(
                `/discover?show=${show.id}${next === null ? "" : `&season=${next}`}`,
                { replace: true },
              );
          }}
        />
      )}
      {season !== null && seasons.isFetching && (
        <Text role="status">Loading season episodes.</Text>
      )}
      {season !== null && unavailable && (
        <Alert color="yellow">
          Season metadata is temporarily unavailable. This does not mean the
          season is empty.
          <Button variant="subtle" onClick={() => void seasons.refetch()}>
            Retry season
          </Button>
        </Alert>
      )}
      {seasons.data?.season && (
        <Stack gap={0} aria-label="Season episodes">
          {!seasons.data.season.episodes.length && (
            <Text>This season has no listed episodes.</Text>
          )}
          {seasons.data.season.episodes.map((row) => (
            <Group
              key={row.id}
              className={styles.episodeRow}
              justify="space-between"
            >
              <Anchor
                c="light-dark(var(--mantine-color-brand-7), var(--mantine-color-brand-4))"
                component={Link}
                to={episodeLink(show.id, row.season, row.episode)}
                aria-current={row.episode === number ? "page" : undefined}
              >
                {row.season === 0 ? "Special" : `S${row.season}`} E{row.episode}
                : {row.title}
              </Anchor>
              <Text size="sm">{row.air_date ?? "Air date unavailable"}</Text>
            </Group>
          ))}
        </Stack>
      )}
      {number !== null && details.isFetching && (
        <Text role="status">Loading exact episode identity.</Text>
      )}
      {number !== null &&
        (details.isError ||
          details.data?.status === "unavailable" ||
          details.data?.service_status === "unavailable") && (
          <Alert color="yellow">
            {details.data?.unavailable_dependency === "tvdb_episode"
              ? "TVDB episode verification is temporarily unavailable."
              : "Episode metadata is temporarily unavailable."}{" "}
            Your selection is retained.
            {details.data?.status === "cached" && details.data.fetched_at && (
              <Text size="sm">
                Cached identity fetched{" "}
                <time dateTime={details.data.fetched_at}>
                  {new Date(details.data.fetched_at).toLocaleString()}
                </time>
                .
              </Text>
            )}
            <Button variant="subtle" onClick={() => void details.refetch()}>
              Retry episode
            </Button>
          </Alert>
        )}
      {episode && (
        <Stack gap="xs" aria-label="Selected episode">
          <Title order={4}>
            S{episode.season} E{episode.episode}: {episode.title}
          </Title>
          <Text>
            {episode.air_date
              ? `Original air date: ${episode.air_date}`
              : "Original air date unavailable"}
          </Text>
          <Text size="sm">
            TMDB episode {episode.id}
            {episode.imdb_id ? ` · Episode IMDb ${episode.imdb_id}` : ""}
          </Text>
          {!parentMatches && (
            <Alert color="yellow">
              The show identity changed. This episode must be reconciled with
              the current show before subtitle search can continue.
              <Button variant="subtle" onClick={() => void details.refetch()}>
                Retry episode mapping
              </Button>
            </Alert>
          )}
          {parentMatches && episode.identity_status === "resolved" && (
            <Text size="sm">
              Verified TVDB default order: season {episode.target_season},
              episode {episode.target_episode}. Subtitle catalogs can use
              different orders. Timing compatibility is unverified.
            </Text>
          )}
          {parentMatches && episode.identity_status === "unverified" && (
            <Alert color="yellow">
              Episode numbering is unverified. Confirm a manual target below to
              search.
            </Alert>
          )}
          {episode.identity_status === "conflict" && (
            <Alert color="red">
              Source episode identities conflict. Subtitle search is blocked.
              Retry details after the source mapping is corrected.
              <Button variant="subtle" onClick={() => void details.refetch()}>
                Retry episode mapping
              </Button>
            </Alert>
          )}
          <Anchor
            c="light-dark(var(--mantine-color-brand-7), var(--mantine-color-brand-4))"
            component={Link}
            to={episodeLink(show.id, episode.season, episode.episode)}
          >
            Exact episode link
          </Anchor>
        </Stack>
      )}
      {show.imdb_id && parentMatches && !conflicting && !draft.manualEntry && (
        <Button variant="subtle" onClick={beginManual}>
          Enter a manual episode
        </Button>
      )}
      {draft.manualEntry && (
        <Alert color="yellow">
          Manual recovery: confirm the series identity and target numbers below.
          This does not verify source numbering.
        </Alert>
      )}
    </Stack>
  );
}
