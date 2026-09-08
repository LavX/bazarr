import { useEffect, useMemo, useRef } from "react";
import { Link, useLocation } from "react-router";
import {
  Alert,
  Anchor,
  Button,
  Group,
  Image,
  NativeSelect,
  Stack,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
import {
  normalizeTitleQuery,
  useDiscoverMetadata,
} from "@/apis/hooks/discover";
import { useDiscover } from "@/contexts/Discover";
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
      style={{ marginBottom: 32 }}
    >
      <Group justify="space-between" mb="md">
        <Title order={2} id="movie-browse-title">
          {shows ? "Explore shows" : "Explore movies"}
        </Title>
        <Anchor
          c="light-dark(var(--mantine-color-brand-7), var(--mantine-color-brand-4))"
          id="discover-metadata-setup"
          component={Link}
          to="/settings/discover"
          py="sm"
          onClick={() => saveReturn("discover-metadata-setup")}
        >
          Discover settings
        </Anchor>
      </Group>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (normalized) updateBrowsing({ suggestionsClosed: false });
        }}
      >
        <NativeSelect
          className={styles.mediaFilter}
          label="Browse media"
          value={browsing.mediaFilter}
          data={[
            { value: "movie", label: "Movies" },
            { value: "show", label: "Shows" },
          ]}
          onChange={(event) =>
            updateBrowsing({
              mediaFilter:
                event.currentTarget.value === "show" ? "show" : "movie",
              suggestionsClosed: false,
            })
          }
        />
        <TextInput
          id="discover-title-query"
          label={shows ? "Search show titles" : "Search movie titles"}
          placeholder="A title, a year to remember"
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
          description="Global title identities and local library candidates. Browsing never searches subtitle providers."
        />
      </form>
      <div aria-live="polite" role="status" style={{ marginBlock: 16 }}>
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
          c="light-dark(var(--mantine-color-brand-7), var(--mantine-color-brand-4))"
          component={Link}
          to="/settings/discover"
          py="sm"
          onClick={() => saveReturn("discover-metadata-setup")}
        >
          Replace TMDB token
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
        aria-label={shows ? "Show candidates" : "Movie candidates"}
      >
        {items.map((movie) => (
          <Group
            key={movie.source_id}
            py="md"
            wrap="nowrap"
            align="start"
            style={{ borderBottom: "1px solid var(--bz-border-interactive)" }}
          >
            {movie.poster_url && (
              <Image
                src={movie.poster_url}
                alt=""
                w={64}
                h={96}
                radius="sm"
                fit="cover"
                loading="lazy"
              />
            )}
            <Stack gap={4} style={{ minWidth: 0, flex: 1 }}>
              <Anchor
                c="light-dark(var(--mantine-color-brand-7), var(--mantine-color-brand-4))"
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
          </Group>
        ))}
      </Stack>
      <Text size="xs" mt="lg" maw="75ch">
        This product uses the TMDB API but is not endorsed or certified by TMDB.
        Metadata does not establish subtitle availability or compatibility.
      </Text>
    </section>
  );
}
