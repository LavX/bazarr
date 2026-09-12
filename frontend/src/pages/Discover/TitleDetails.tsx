import {
  CSSProperties,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { Link, useNavigate } from "react-router";
import { Alert, Button, Menu, Stack, Text, Title } from "@mantine/core";
import {
  faArrowLeft,
  faArrowRight,
  faCheck,
  faChevronDown,
  faCloud,
  faEllipsis,
  faHardDrive,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useArrInstanceLabels } from "@/apis/hooks/arrInstances";
import { useDiscoverMetadata } from "@/apis/hooks/discover";
import { useDiscover } from "@/contexts/Discover";
import EpisodePicker from "./EpisodePicker";
import MediaPoster from "./MediaPoster";
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
  const { details, movie } = useSelectedTitle();
  if (!movie || details.data?.status !== "cached") return null;
  return (
    <Text className={styles.titleNotes}>
      Cached title details
      {details.data.service_status ? ", metadata service unavailable" : ""}.
    </Text>
  );
}

/**
 * The series episode choice, rendered inside the retrieval panel in the page
 * rather than beside the title, so the detail order reads sheet, retrieval,
 * results. Exact identity, invalidation and manual recovery all stay in
 * EpisodePicker.
 */
export function TitleEpisodePicker({
  showOptions = false,
}: {
  showOptions?: boolean;
}) {
  const { movie } = useSelectedTitle();
  if (movie?.media_type === "show" && movie.source === "tmdb") {
    return (
      <EpisodePicker
        key={movie.source_id}
        show={movie}
        showOptions={showOptions}
      />
    );
  }
  return null;
}

export default function TitleDetails({
  onOptions,
  optionsOpen,
}: {
  onOptions: () => void;
  optionsOpen: boolean;
}) {
  const { state, updateBrowsing, updateDraft, cancelPending } = useDiscover();
  const { id, kind, source, details, received, movie } = useSelectedTitle();
  const heading = useRef<HTMLHeadingElement>(null);
  const [failedBackdrop, setFailedBackdrop] = useState<string | null>(null);
  const { nameById } = useArrInstanceLabels(
    kind === "show" ? "sonarr" : "radarr",
  );
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

  const copies = movie?.copies ?? [];
  const inLibrary = movie?.source === "local" || copies.length > 0;
  const localIds = copies.length
    ? copies.map((copy) => copy.local_id)
    : movie?.source === "local"
      ? [Number(movie.id)]
      : [];
  const libraryDestinations = [...new Set(localIds)]
    .filter((localId) => Number.isSafeInteger(localId) && localId > 0)
    .map((localId, index) => {
      const copy = copies.find((item) => item.local_id === localId);
      const instanceName =
        copy?.arr_instance_id != null
          ? nameById.get(copy.arr_instance_id)
          : undefined;
      return {
        to: `/${kind === "show" ? "series" : "movies"}/${localId}`,
        label:
          instanceName ??
          (copy?.arr_instance_id != null
            ? `Library ${copy.arr_instance_id}`
            : `Library copy ${index + 1}`),
      };
    });
  const destinations = libraryDestinations.map((destination) => ({
    ...destination,
    label:
      libraryDestinations.filter((other) => other.label === destination.label)
        .length > 1
        ? `${destination.label} · Copy ${destination.to.split("/").pop()}`
        : destination.label,
  }));
  const libraryUncertain =
    !inLibrary &&
    (movie?.copies === undefined ||
      movie.copies_truncated ||
      movie.ownership?.truncated ||
      !received ||
      details.isError);
  const libraryLabel = inLibrary
    ? "In your library"
    : libraryUncertain
      ? details.isFetching
        ? "Checking your library…"
        : "Library check incomplete"
      : "Not in your library";
  const hasBackdrop = Boolean(
    movie?.backdrop_url && failedBackdrop !== movie.backdrop_url,
  );
  return (
    <Stack gap={8} mb={16}>
      <div className={styles.titleTools}>
        <Button
          variant="default"
          className={styles.detailBack}
          leftSection={<FontAwesomeIcon icon={faArrowLeft} />}
          onClick={() => {
            // Leaving the title retires its pending subtitle work. Filed
            // results stay for the shared form; only the in-flight response
            // loses its owner.
            cancelPending();
            updateBrowsing({ selectedId: null, suggestionsClosed: false });
            void navigate("/discover");
          }}
        >
          Back to Discover
        </Button>
        <Button
          type="button"
          variant="subtle"
          className={styles.detailOptions}
          aria-label="Search options"
          aria-expanded={optionsOpen}
          onClick={onOptions}
        >
          <FontAwesomeIcon icon={faEllipsis} />
        </Button>
      </div>
      {movie ? (
        <div
          className={styles.detailHero}
          data-artwork={hasBackdrop}
          style={
            hasBackdrop
              ? ({
                  "--detail-backdrop": `url("${movie.backdrop_url}")`,
                } as CSSProperties)
              : undefined
          }
        >
          <article
            className={styles.detailHeroSurface}
            aria-label={`${movie.title} details`}
          >
            {hasBackdrop && (
              <img
                className={styles.detailBackdrop}
                src={movie.backdrop_url!}
                alt=""
                onError={() => setFailedBackdrop(movie.backdrop_url)}
              />
            )}
            <span className={styles.detailPoster} aria-hidden="true">
              <MediaPoster key={movie.poster_url} src={movie.poster_url} />
            </span>
            <div className={styles.detailHeroCopy}>
              <div className={styles.detailHeroHeading}>
                <Title
                  order={2}
                  tabIndex={-1}
                  ref={heading}
                  className={styles.detailTitle}
                >
                  {movie.title}
                </Title>
                <Text className={styles.detailMeta}>
                  {movie.year ?? "Year unavailable"} ·{" "}
                  {movie.media_type === "show" ? "Series" : "Film"}
                </Text>
              </div>
              <Text className={styles.detailOverview}>
                {movie.overview || "No overview is available for this title."}
              </Text>
              <div className={styles.detailAvailability}>
                <span className={styles.libraryStatus} data-local={inLibrary}>
                  <FontAwesomeIcon icon={inLibrary ? faCheck : faCloud} />
                  {libraryLabel}
                </span>
                {destinations.length === 1 && (
                  <Button
                    component={Link}
                    to={destinations[0].to}
                    className={styles.openLibrary}
                    rightSection={<FontAwesomeIcon icon={faArrowRight} />}
                  >
                    Open in library
                  </Button>
                )}
                {destinations.length > 1 && (
                  <Menu position="bottom-start" withinPortal>
                    <Menu.Target>
                      <Button
                        className={styles.openLibrary}
                        rightSection={<FontAwesomeIcon icon={faChevronDown} />}
                      >
                        Open in library
                      </Button>
                    </Menu.Target>
                    <Menu.Dropdown>
                      <Menu.Label>Choose a library</Menu.Label>
                      {destinations.map((destination) => (
                        <Menu.Item
                          key={destination.to}
                          component={Link}
                          to={destination.to}
                          leftSection={<FontAwesomeIcon icon={faHardDrive} />}
                        >
                          {destination.label}
                        </Menu.Item>
                      ))}
                    </Menu.Dropdown>
                  </Menu>
                )}
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
          Set up TMDB in the Subtitle Hub to load global title details. IMDb
          subtitle search remains available in Search options.
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
          unavailable for this selection. You can enter a verified IMDb ID in
          Search options.
        </Alert>
      )}
      {movie?.media_type === "show" && movie.source !== "tmdb" && (
        <Alert color="yellow">
          This source does not provide verified episode numbering. Confirm the
          series IMDb ID and enter the season and episode below.
        </Alert>
      )}
    </Stack>
  );
}
