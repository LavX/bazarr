import { useEffect, useMemo, useRef } from "react";
import { Link, useLocation } from "react-router";
import {
  Alert,
  Anchor,
  Button,
  SegmentedControl,
  Stack,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
import { faMagnifyingGlass } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  normalizeTitleQuery,
  useDiscoverMetadata,
} from "@/apis/hooks/discover";
import { useDiscover } from "@/contexts/Discover";
import MediaPoster from "./MediaPoster";
import styles from "./Discover.module.scss";

export default function TitleSearch() {
  const { state, updateBrowsing, updateDraft } = useDiscover();
  const { browsing } = state;
  const location = useLocation();
  const shows = browsing.mediaFilter === "show";
  const normalized = normalizeTitleQuery(browsing.query);
  const [debounced] = useDebouncedValue(normalized, 300);
  const [localQuery] = useDebouncedValue(browsing.query.trim(), 300);
  const results = useDiscoverMetadata(
    "search",
    debounced,
    Boolean(debounced) &&
      normalized === debounced &&
      localQuery === browsing.query.trim() &&
      !browsing.suggestionsClosed,
    browsing.mediaFilter,
    "all",
    localQuery,
  );
  const status = useDiscoverMetadata("status");
  const root = useRef<HTMLElement>(null);
  const restored = useRef(false);
  const restoreFocus = browsing.focusId;
  const scrollY = browsing.scrollY;
  const items = useMemo(
    () =>
      normalized &&
      normalized === debounced &&
      localQuery === browsing.query.trim() &&
      !browsing.suggestionsClosed
        ? (results.data?.items ?? [])
        : [],
    [
      normalized,
      debounced,
      localQuery,
      browsing.query,
      browsing.suggestionsClosed,
      results.data,
    ],
  );
  const source = normalized && results.data ? results.data : status.data;

  useEffect(() => {
    if (restored.current) return;
    const node = document.getElementById(restoreFocus);
    if (node && root.current?.contains(node)) {
      restored.current = true;
      node.focus({ preventScroll: true });
      window.scrollTo({ top: scrollY, behavior: "instant" });
    }
  }, [restoreFocus, scrollY, items]);

  const saveReturn = (focusId: string) =>
    updateBrowsing({
      returnTarget: location.pathname + location.search + location.hash,
      focusId,
      scrollY: window.scrollY,
    });

  return (
    <section
      ref={root}
      aria-labelledby="movie-browse-title"
      className={styles.search}
    >
      {/* The section keeps its heading for assistive technology. On screen the
          bar is the heading: one control, first thing on the page. */}
      <Title
        order={2}
        id="movie-browse-title"
        className={styles.visuallyHidden}
      >
        {shows ? "Explore shows" : "Explore movies"}
      </Title>
      <form
        className={styles.searchBar}
        onSubmit={(event) => {
          event.preventDefault();
          if (normalized) updateBrowsing({ suggestionsClosed: false });
        }}
      >
        {/* This scopes the title search, not the feeds below it. One
            vocabulary with the trending tabs: Movies and Series. A segmented
            pair shows both answers; arrow keys move between them. */}
        <div className={styles.searchScope}>
          <SegmentedControl
            aria-label="Search for"
            classNames={{
              root: styles.segmented,
              label: styles.segmentedLabel,
              indicator: styles.segmentedIndicator,
            }}
            value={browsing.mediaFilter}
            data={[
              { value: "movie", label: "Movies" },
              { value: "show", label: "Series" },
            ]}
            onChange={(value) =>
              updateBrowsing({
                mediaFilter: value === "show" ? "show" : "movie",
                suggestionsClosed: false,
              })
            }
          />
        </div>
        <TextInput
          id="discover-title-query"
          className={styles.searchField}
          classNames={{ label: styles.visuallyHidden }}
          label={shows ? "Search series titles" : "Search movie titles"}
          placeholder={shows ? "Search series titles" : "Search movie titles"}
          leftSection={<FontAwesomeIcon icon={faMagnifyingGlass} />}
          maxLength={200}
          value={browsing.query}
          autoComplete="off"
          onChange={(event) =>
            updateBrowsing({
              query: event.currentTarget.value,
              suggestionsClosed: false,
              focusId: "discover-title-query",
            })
          }
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              event.stopPropagation();
              updateBrowsing({ suggestionsClosed: true });
            }
          }}
          aria-controls="discover-title-suggestions"
          aria-expanded={items.length > 0}
          aria-describedby="discover-title-hint"
        />
      </form>
      <div className={styles.searchFoot}>
        <Text component="p" id="discover-title-hint">
          Global title identities and local library candidates. Browsing never
          searches subtitle providers.
        </Text>
        <Anchor
          id="discover-metadata-setup"
          component={Link}
          to="/settings/discover"
          onClick={() => saveReturn("discover-metadata-setup")}
        >
          Discover settings
        </Anchor>
      </div>
      <div aria-live="polite" role="status" className={styles.searchStatus}>
        {status.settingsLoading && <Text>Loading Discover setup.</Text>}
        {status.settingsError && (
          <Alert color="yellow">
            Discover setup could not be loaded. Reload this page to retry. IMDb
            subtitle search remains available below.
          </Alert>
        )}
        {!status.settingsLoading &&
          !status.settingsError &&
          !status.configured && (
            <Alert color="yellow">
              Set up TMDB in Discover settings to browse movies beyond your
              library. IMDb subtitle search and your local library remain
              available.
            </Alert>
          )}
        {source &&
          ["authentication_failed", "unavailable"].includes(source.status) && (
            <Alert color="yellow">{source.message}</Alert>
          )}
        {(status.isError || results.isError) && (
          <Alert color="yellow">
            Movie metadata could not be loaded. Retry the search.
          </Alert>
        )}
        {Boolean(normalized) &&
          status.configured &&
          normalized === debounced &&
          results.isFetching && <Text>Searching movie titles.</Text>}
        {source?.status === "cached" && (
          <Text size="sm">
            Cached {source.source.toUpperCase()} metadata
            {source.service_status
              ? `, ${source.source.toUpperCase()} is temporarily unavailable`
              : ""}
            .{" "}
            {source.fetched_at && (
              <time dateTime={source.fetched_at}>
                {new Date(source.fetched_at).toLocaleString()}
              </time>
            )}
          </Text>
        )}
        {results.data &&
          ["available", "cached"].includes(results.data.status) &&
          normalized === debounced &&
          !items.length &&
          !browsing.suggestionsClosed &&
          Boolean(normalized) && (
            <Text>
              No {shows ? "shows" : "movies"} matched this title. Try another
              title.
            </Text>
          )}
      </div>
      {(status.isError ||
        results.isError ||
        source?.status === "unavailable") && (
        <Button
          variant="light"
          mb="md"
          onClick={() => {
            void status.refetch();
            if (normalized) void results.refetch();
          }}
        >
          Retry metadata
        </Button>
      )}
      {source?.status === "authentication_failed" && (
        <Anchor
          c="var(--discover-link)"
          component={Link}
          to="/settings/discover"
          py="sm"
          onClick={() => saveReturn("discover-metadata-setup")}
        >
          Check the TMDB key
        </Anchor>
      )}
      {source?.primary &&
        source.source !== "tmdb" &&
        (["unavailable", "authentication_failed"].includes(
          source.primary.status,
        ) ||
          source.primary.service_status === "unavailable") && (
          <Alert color="yellow" mb="md">
            {source.primary.service_status === "unavailable"
              ? "TMDB is temporarily unavailable. Cached catalog records may be shown."
              : source.primary.message}{" "}
            <Button variant="subtle" onClick={() => void results.refetch()}>
              Retry metadata
            </Button>
          </Alert>
        )}
      {source?.fallback &&
        (["unavailable", "authentication_failed"].includes(
          source.fallback.status,
        ) ||
          source.fallback.failure_reason) && (
          <Alert color="yellow" mb="md">
            {source.fallback.message}
          </Alert>
        )}
      {(results.data?.local_truncated || results.data?.truncated) && (
        <Text size="sm" mb="md">
          Showing a limited set of matches. Narrow the title to see other
          candidates.
        </Text>
      )}
      {normalized && !items.length && !results.isFetching && (
        <Text size="sm" mb="md">
          Try metadata setup or retry, or choose Search providers by release
          name below. Provider search requires Find subtitles.
        </Text>
      )}
      <Stack
        id="discover-title-suggestions"
        gap={0}
        className={styles.candidates}
        aria-label={shows ? "Show candidates" : "Movie candidates"}
      >
        {items.map((movie) => (
          <div key={movie.source_id} className={styles.candidate}>
            <span aria-hidden="true">
              <MediaPoster key={movie.poster_url} src={movie.poster_url} />
            </span>
            <Stack gap={4} style={{ minWidth: 0 }}>
              <Anchor
                c="var(--discover-link)"
                component="button"
                type="button"
                ta="left"
                fw={650}
                id={`discover-${movie.source}-${movie.media_type}-${movie.id}`}
                style={{ minHeight: 44, overflowWrap: "anywhere" }}
                onClick={() => {
                  saveReturn(
                    `discover-${movie.source}-${movie.media_type}-${movie.id}`,
                  );
                  const reopen =
                    browsing.identityLoaded &&
                    browsing.adoptedSourceId === movie.source_id;
                  updateBrowsing({
                    selectedId: movie.id,
                    selectedSource: movie.source,
                    selectedType: movie.media_type,
                    selectedSeason: reopen ? browsing.selectedSeason : null,
                    selectedEpisode: reopen ? browsing.selectedEpisode : null,
                    adoptedSourceId: reopen ? movie.source_id : null,
                    identityLoaded: reopen,
                    adoptedMovieId: reopen ? movie.id : null,
                  });
                  if (!reopen) {
                    updateDraft({
                      mediaType:
                        movie.media_type === "show" ? "episode" : "movie",
                      showId:
                        movie.media_type === "show" && movie.source === "tmdb"
                          ? movie.id
                          : undefined,
                      showTvdbId: undefined,
                      manualEntry: movie.source !== "tmdb",
                      episodeIdentity: undefined,
                      manualConfirmed: false,
                      season: "",
                      episode: "",
                      imdbId: "",
                      title: movie.title,
                      year: movie.year ?? undefined,
                    });
                  }
                }}
              >
                {movie.title}
                {movie.year ? ` (${movie.year})` : ""}
              </Anchor>
              <Text size="sm">
                {movie.media_type === "show" ? "Show" : "Movie"} ·{" "}
                {movie.source === "local"
                  ? "Local library"
                  : movie.source.toUpperCase()}
                {movie.provenance === "cached" ? " · Cached" : ""}
                {movie.imdb_id
                  ? ` · ${movie.imdb_id}`
                  : " · IMDb mapping checked in details"}
              </Text>
              {Boolean(movie.copies?.length) && (
                <Text size="sm">
                  {movie.copies?.length} local{" "}
                  {movie.copies?.length === 1 ? "copy" : "copies"}
                  {movie.copies_truncated ? " or more" : ""}. Selection uses
                  title identity only.
                </Text>
              )}
              {movie.overview && (
                <Text size="sm" lineClamp={2} maw="70ch">
                  {movie.overview}
                </Text>
              )}
            </Stack>
          </div>
        ))}
      </Stack>
      <Text component="p" className={styles.searchCaveat}>
        {/* Two separate statements. The homepage renders one shared source
            attribution for the whole page, so it hides this copy of that
            sentence by name, and the availability caveat stays visible. */}
        <span className={styles.sourceAttribution}>
          This product uses the TMDB API but is not endorsed or certified by
          TMDB.{" "}
        </span>
        Metadata does not establish subtitle availability or compatibility.
      </Text>
    </section>
  );
}
