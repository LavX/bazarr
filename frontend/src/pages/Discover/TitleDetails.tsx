import { CSSProperties, useEffect, useLayoutEffect, useRef } from "react";
import { Link, useLocation, useNavigate } from "react-router";
import { Alert, Anchor, Button, Stack, Text, Title } from "@mantine/core";
import { useDiscoverMetadata } from "@/apis/hooks/discover";
import { useDiscover } from "@/contexts/Discover";
import EpisodePicker from "./EpisodePicker";
import MediaPoster, { Backdrop } from "./MediaPoster";
import styles from "./Discover.module.scss";

/** The selected title as the page currently knows it, cached or received. */
function useSelectedTitle() {
  const { state } = useDiscover();
  const id = state.browsing.selectedId;
  const kind = state.browsing.selectedType;
  const source = state.browsing.selectedSource;
  const details = useDiscoverMetadata(
    `${kind === "show" ? "shows" : "movies"}/${id}`,
    undefined,
    id !== null,
    kind,
    source,
  );
  const received = details.data?.item;
  const retained = state.browsing.retainedTitle;
  const movie =
    received ??
    (retained?.id === id &&
    retained.media_type === kind &&
    retained.source === source
      ? retained
      : null);
  return { id, kind, source, details, received, movie };
}

/**
 * What the metadata can and cannot tell the reader. Honest, kept, and placed
 * after the search rather than between the reader and it.
 */
export function TitleNotes() {
  const { kind, details, movie } = useSelectedTitle();
  if (!movie) return null;
  return (
    <div className={styles.titleNotes}>
      {details.data?.status === "cached" && (
        <Text>
          Cached {movie.source.toUpperCase()} metadata
          {details.data.service_status
            ? `, ${movie.source.toUpperCase()} is temporarily unavailable`
            : ""}
          .
        </Text>
      )}
      {details.data?.fetched_at && (
        <Text>
          Metadata fetched{" "}
          <time dateTime={details.data.fetched_at}>
            {new Date(details.data.fetched_at).toLocaleString()}
          </time>
          .
        </Text>
      )}
      <Text>
        Metadata does not establish subtitle availability, timing compatibility
        or video playback.
      </Text>
      {Boolean(movie.copies?.length) && (
        <>
          <Text>Local library copies</Text>
          {movie.copies?.map((copy) => (
            <Text key={copy.local_id}>
              Local {kind} {copy.local_id} ·{" "}
              {copy.arr_instance_id === null
                ? "Owner unknown"
                : `Arr instance ${copy.arr_instance_id}`}
              {kind === "show"
                ? ` · ${copy.episode_count === null ? "Episode ownership unknown" : `${copy.episode_count} stored episode rows`}`
                : ""}
            </Text>
          ))}
          {movie.copies_truncated && (
            <Text>Additional copies may exist. This list is limited.</Text>
          )}
          <Text>
            Title identity only. No file, filename or hash is selected.
          </Text>
        </>
      )}
      {movie.media_type === "show" && movie.ownership && (
        <Text>
          {movie.ownership.episode_count} stored episode rows across the listed
          copies with known owners. This does not establish ownership of the
          selected episode, every season or playable files.
        </Text>
      )}
    </div>
  );
}

export default function TitleDetails() {
  const { state, updateBrowsing, updateDraft } = useDiscover();
  const { id, kind, source, details, received, movie } = useSelectedTitle();
  const heading = useRef<HTMLHeadingElement>(null);
  const location = useLocation();
  const navigate = useNavigate();
  const currentDraft = useRef(state.draft);
  currentDraft.current = state.draft;

  useEffect(() => {
    heading.current?.focus();
  }, [id, kind, source]);
  useLayoutEffect(() => {
    if (
      received &&
      movie &&
      movie.id === id &&
      movie.media_type === kind &&
      movie.source === source
    ) {
      // The reducer preserves accepted results for an unchanged effective
      // subtitle context and retires them when fresh details change it.
      const draft = currentDraft.current;
      const parentChanged =
        movie.media_type === "show" &&
        (draft.showId === movie.id ||
          state.browsing.adoptedSourceId === movie.source_id) &&
        (draft.imdbId !== (movie.imdb_id ?? "") ||
          draft.showTvdbId !== movie.tvdb_id ||
          draft.title !== movie.title ||
          draft.year !== (movie.year ?? undefined));
      updateDraft({
        mediaType: movie.media_type === "show" ? "episode" : "movie",
        showId:
          movie.media_type === "show" && movie.source === "tmdb"
            ? movie.id
            : undefined,
        showTvdbId: movie.media_type === "show" ? movie.tvdb_id : undefined,
        imdbId: movie.imdb_id ?? "",
        title: movie.title,
        year: movie.year ?? undefined,
        ...(movie.source !== "tmdb"
          ? { episodeIdentity: undefined, manualEntry: true }
          : {}),
        ...(parentChanged
          ? {
              season: "",
              episode: "",
              manualConfirmed: false,
              manualEntry: false,
            }
          : {}),
      });
      updateBrowsing({
        identityLoaded: true,
        adoptedMovieId: movie.media_type === "movie" ? movie.id : null,
        adoptedSourceId: movie.source_id,
        retainedTitle: movie,
      });
    }
  }, [
    received,
    movie,
    id,
    kind,
    source,
    state.browsing.adoptedSourceId,
    updateDraft,
    updateBrowsing,
  ]);

  const artwork = movie?.backdrop_url ? "true" : "false";
  return (
    <Stack gap="md" mb={16}>
      <div className={styles.titleTools}>
        <Button
          variant="subtle"
          onClick={() => {
            updateBrowsing({ selectedId: null });
            if (kind === "show") void navigate("/discover");
          }}
        >
          Back to{" "}
          {state.browsing.focusId.startsWith("discover-trending-")
            ? "Discover"
            : kind === "show"
              ? "shows"
              : "movies"}
        </Button>
        <Anchor
          c="var(--discover-link)"
          component={Link}
          to="/settings/discover"
          py="sm"
          onClick={() =>
            updateBrowsing({
              returnTarget: location.pathname + location.search + location.hash,
            })
          }
        >
          Discover settings
        </Anchor>
      </div>
      {/* The title has the presence the homepage hero has: its own backdrop,
          its poster, and a heading set like one. The metadata that cannot
          promise anything about subtitles sits in the margin underneath. */}
      {movie ? (
        <div
          className={styles.stage}
          data-artwork={artwork}
          style={
            movie.backdrop_url
              ? ({
                  "--feature-backdrop": `url("${movie.backdrop_url}")`,
                } as CSSProperties)
              : undefined
          }
        >
          <article
            className={`${styles.feature} ${styles.titleFeature}`}
            data-artwork={artwork}
          >
            <Backdrop key={movie.backdrop_url} src={movie.backdrop_url} />
            <div className={styles.titleFeatureCopy}>
              <span className={styles.titlePoster} aria-hidden="true">
                <MediaPoster key={movie.poster_url} src={movie.poster_url} />
              </span>
              <div>
                <Title
                  order={2}
                  tabIndex={-1}
                  ref={heading}
                  style={{ overflowWrap: "anywhere" }}
                >
                  {movie.title}
                </Title>
                <Text className={styles.featureMeta}>
                  {movie.media_type === "show" ? "Series" : "Film"}
                  {movie.year ? ` · ${movie.year}` : ""} ·{" "}
                  {movie.source === "local"
                    ? "Local library"
                    : movie.source.toUpperCase()}
                  {movie.imdb_id ? ` · IMDb ${movie.imdb_id}` : ""}
                </Text>
                <Text className={styles.featureOverview} lineClamp={3}>
                  {movie.overview ||
                    (movie.media_type === "show"
                      ? "No overview is available for this show."
                      : "No overview is available for this movie.")}
                </Text>
              </div>
            </div>
          </article>
        </div>
      ) : (
        <Title
          order={2}
          tabIndex={-1}
          ref={heading}
          style={{ overflowWrap: "anywhere" }}
        >
          {kind === "show" ? "Show details" : "Movie details"}
        </Title>
      )}
      {details.isFetching && (
        <Text role="status">
          {kind === "show" ? "Loading show details." : "Loading movie details."}
        </Text>
      )}
      {!details.configured && (
        <Alert color="yellow">
          Set up TMDB in Discover settings to load global movie details. IMDb
          subtitle search remains available below.
        </Alert>
      )}
      {(details.isError ||
        details.data?.status === "unavailable" ||
        details.data?.failure_reason) && (
        <Alert color="yellow">
          {details.data?.failure_reason
            ? details.data.message
            : kind === "show"
              ? "Show details are temporarily unavailable. Your selection is retained."
              : "Movie details are temporarily unavailable."}{" "}
          <Button variant="subtle" onClick={() => void details.refetch()}>
            Retry details
          </Button>
        </Alert>
      )}
      {details.primaryMetadata &&
        ["unavailable", "authentication_failed"].includes(
          details.primaryMetadata.status,
        ) && <Alert color="yellow">{details.primaryMetadata.message}</Alert>}
      {details.data?.status === "authentication_failed" && (
        <Alert color="yellow">
          {source.toUpperCase()} rejected the key Discover is using. Check it in
          settings.
        </Alert>
      )}
      {movie && !movie.imdb_id && (
        <Alert color="yellow">
          {movie.source === "local"
            ? "This local title"
            : movie.source.toUpperCase()}{" "}
          has no resolved IMDb identity for this title. Subtitle lookup is
          unavailable for this selection. You can enter a verified IMDb ID
          below.
        </Alert>
      )}
      {movie?.media_type === "show" && movie.source !== "tmdb" && (
        <Alert color="yellow">
          This source does not provide verified episode numbering. Confirm the
          series IMDb ID and enter the season and episode below.
        </Alert>
      )}
      {movie?.media_type === "show" && movie.source === "tmdb" && (
        <EpisodePicker key={movie.source_id} show={movie} />
      )}
    </Stack>
  );
}
