import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router";
import {
  Alert,
  Anchor,
  Button,
  Group,
  Stack,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
import {
  faArrowRight,
  faMagnifyingGlass,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  normalizeTitleQuery,
  useDiscoverMetadata,
} from "@/apis/hooks/discover";
import { useServerSearch } from "@/apis/hooks/system";
import { useDiscover } from "@/contexts/Discover";
import { useSearchSources } from "@/contexts/UniversalSearch";
import MediaPoster from "@/pages/Discover/MediaPoster";
import { discoverTitlePath } from "@/pages/Discover/navigation";
import { useRouteItems } from "@/Router";
import { CustomRouteObject } from "@/Router/type";
import { pathJoin } from "@/utilities";
import styles from "./UniversalSearch.module.scss";

// The search field carries the approved concept's bh-query identity in the
// DOM, while the saved return-focus identity keeps its long-standing
// discover prefix. State persists only for the visit, but a return saved
// before this composition still resolves through the alias below.
export const TITLE_QUERY_INPUT_ID = "bh-query";
export const TITLE_QUERY_FOCUS_ID = "discover-title-query";
export function resolveTitleQueryTarget(focusId: string): string {
  return focusId === TITLE_QUERY_FOCUS_ID ? TITLE_QUERY_INPUT_ID : focusId;
}

export default function UniversalSearch() {
  const { state, updateBrowsing, updateDraft } = useDiscover();
  const { browsing } = state;
  const location = useLocation();
  const navigate = useNavigate();
  const onDiscover = location.pathname.replace(/\/$/, "") === "/discover";
  const [focused, setFocused] = useState(false);
  const sources = useSearchSources();
  const panelOpen =
    !browsing.suggestionsClosed &&
    (Boolean(browsing.query.trim()) ||
      focused ||
      (!browsing.manualSearch &&
        browsing.focusId.startsWith("discover-manual-")));
  const shows = false;
  const allMedia = true;
  const normalized = normalizeTitleQuery(browsing.query);
  const [debounced] = useDebouncedValue(normalized, 300);
  const [localQuery] = useDebouncedValue(browsing.query.trim(), 300);
  const searching =
    Boolean(debounced) &&
    normalized === debounced &&
    localQuery === browsing.query.trim() &&
    !browsing.suggestionsClosed;
  const movies = useDiscoverMetadata(
    "search",
    debounced,
    searching && !shows,
    "movie",
    "all",
    localQuery,
  );
  const series = useDiscoverMetadata(
    "search",
    debounced,
    searching && (allMedia || shows),
    "show",
    "all",
    localQuery,
  );
  const library = useServerSearch(localQuery, searching);
  const localMatches = searching
    ? (library.data ?? [])
        .filter(
          (item) =>
            item.id != null &&
            (item.sonarrSeriesId != null || item.radarrId != null),
        )
        .slice(0, 6)
    : [];
  const contextualMatches = sources.map((source) => ({
    ...source,
    matches: panelOpen ? source.search(browsing.query) : [],
  }));
  const routes = useRouteItems();
  const navigationPages = useMemo(() => {
    const pages: { title: string; path: string; terms: string }[] = [];
    function collect(items: CustomRouteObject[], parent = "/", context = "") {
      for (const route of items) {
        if (
          route.hidden ||
          !route.path ||
          route.path.includes(":") ||
          route.path.includes("*")
        )
          continue;
        const path = pathJoin(parent, route.path);
        if (
          route.name &&
          (route.element || route.children?.some((child) => child.index))
        )
          pages.push({
            title: context ? `${context}: ${route.name}` : route.name,
            path,
            terms: "",
          });
        if (route.children)
          collect(route.children, path, route.name ?? context);
      }
    }
    collect(routes);
    return pages;
  }, [routes]);
  const appRoutes = routes.find((route) => route.path === "/")?.children ?? [];
  const libraryPage = (kind: string) =>
    appRoutes.some(
      (route: CustomRouteObject) => route.path === kind && route.hidden,
    )
      ? "/settings/connections"
      : `/${kind}`;
  const pages = [
    { title: "Discover", path: "/discover", terms: "browse catalog" },
    { title: "Series", path: libraryPage("series"), terms: "shows library" },
    { title: "Movies", path: libraryPage("movies"), terms: "films library" },
    { title: "Providers", path: "/subtitle-hub", terms: "subtitles providers" },
    {
      title: "Settings",
      path: "/settings/general",
      terms: "preferences configuration",
    },
    { title: "Activity", path: "/system/tasks", terms: "jobs tasks" },
    ...navigationPages.filter(
      (page) =>
        ![
          "/discover",
          "/series",
          "/movies",
          "/subtitle-hub",
          "/settings/general",
          "/system/tasks",
        ].includes(page.path),
    ),
  ].filter(
    (page) =>
      normalized &&
      `${page.title} ${page.terms}`
        .toLowerCase()
        .includes(normalized.toLowerCase()),
  );
  const results = shows ? series : movies;
  const additional = allMedia ? series : undefined;
  const fetching = results.isFetching || Boolean(additional?.isFetching);
  const status = useDiscoverMetadata("status");
  const root = useRef<HTMLElement>(null);
  useEffect(() => {
    const focusSearch = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        document.getElementById(TITLE_QUERY_INPUT_ID)?.focus();
        updateBrowsing({ suggestionsClosed: false });
      }
    };
    window.addEventListener("keydown", focusSearch);
    return () => window.removeEventListener("keydown", focusSearch);
  }, [updateBrowsing]);
  useEffect(() => {
    const close = (event: PointerEvent) => {
      if (
        event.target instanceof Node &&
        !root.current?.contains(event.target)
      ) {
        setFocused(false);
        updateBrowsing({ suggestionsClosed: true });
      }
    };
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, [updateBrowsing]);
  const restored = useRef(false);
  const restoreFocus = browsing.focusId;
  const scrollY = browsing.scrollY;
  const items = useMemo(
    () =>
      normalized &&
      normalized === debounced &&
      localQuery === browsing.query.trim() &&
      !browsing.suggestionsClosed
        ? Array.from(
            new Map(
              Array.from(
                {
                  length: Math.max(
                    results.data?.items?.length ?? 0,
                    additional?.data?.items?.length ?? 0,
                  ),
                },
                (_, index) => [
                  results.data?.items?.[index],
                  additional?.data?.items?.[index],
                ],
              ).flatMap((pair) =>
                pair.flatMap((item) =>
                  item ? [[item.source_id, item] as const] : [],
                ),
              ),
            ).values(),
          )
        : [],
    [
      normalized,
      debounced,
      localQuery,
      browsing.query,
      browsing.suggestionsClosed,
      results.data,
      additional?.data,
    ],
  );
  const hasOtherMatches =
    localMatches.length > 0 ||
    pages.length > 0 ||
    contextualMatches.some((source) => source.matches.length > 0);
  const source = normalized && results.data ? results.data : status.data;
  const fallbackAvailable =
    status.settingsError ||
    (!status.settingsLoading && !status.configured) ||
    status.isError ||
    results.isError ||
    additional?.isError ||
    source?.status === "unavailable" ||
    source?.status === "authentication_failed" ||
    (Boolean(normalized) &&
      !items.length &&
      !fetching &&
      !results.isPending &&
      !additional?.isPending);

  useEffect(() => {
    if (browsing.selectedId !== null || browsing.manualSearch) {
      restored.current = false;
      return;
    }
    if (
      !onDiscover ||
      restored.current ||
      (!browsing.query &&
        !scrollY &&
        !browsing.focusId.startsWith("discover-manual-"))
    )
      return;
    const node = document.getElementById(resolveTitleQueryTarget(restoreFocus));
    if (node && root.current?.contains(node)) {
      restored.current = true;
      node.focus({ preventScroll: true });
      window.scrollTo({ top: scrollY, behavior: "instant" });
    }
  }, [
    restoreFocus,
    scrollY,
    items,
    fallbackAvailable,
    browsing.selectedId,
    browsing.manualSearch,
    browsing.focusId,
    browsing.query,
    onDiscover,
  ]);

  const saveReturn = (focusId: string) =>
    updateBrowsing({
      returnTarget: location.pathname + location.search + location.hash,
      focusId,
      scrollY: window.scrollY,
    });
  const openManualSearch = (mode: "title" | "release", focusId: string) => {
    saveReturn(focusId);
    updateBrowsing({ suggestionsClosed: true });
    void navigate(`/discover?mode=${mode}`);
    updateBrowsing({
      manualSearch: true,
      selectedId: null,
      retainedTitle: null,
      identityLoaded: false,
      adoptedSourceId: null,
      adoptedMovieId: null,
      releaseContext: null,
      recentContext: null,
    });
    updateDraft({
      mode,
      query: mode === "release" ? browsing.query.trim() : "",
      imdbId: /^tt\d{7,10}$/.test(browsing.query.trim())
        ? browsing.query.trim()
        : "",
      mediaType: shows ? "episode" : "movie",
      title: undefined,
      year: undefined,
      showId: undefined,
      showTvdbId: undefined,
      episodeIdentity: undefined,
      manualEntry: true,
      manualConfirmed: false,
      season: "",
      episode: "",
      copyId: undefined,
    });
  };

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
        {allMedia
          ? "Explore movies and shows"
          : shows
            ? "Explore shows"
            : "Explore movies"}
      </Title>
      <div className={styles.searchArea}>
        <form
          className={styles.searchBar}
          onSubmit={(event) => {
            event.preventDefault();
            if (browsing.query.trim())
              updateBrowsing({ suggestionsClosed: false });
          }}
        >
          <TextInput
            id={TITLE_QUERY_INPUT_ID}
            className={styles.searchField}
            classNames={{ label: styles.visuallyHidden }}
            label="Search"
            placeholder={
              sources.length
                ? "Search titles, pages, or subtitle text…"
                : "Search titles, library, and more…"
            }
            leftSection={<FontAwesomeIcon icon={faMagnifyingGlass} />}
            maxLength={200}
            value={browsing.query}
            autoComplete="off"
            onFocus={() => {
              setFocused(true);
              updateBrowsing({ suggestionsClosed: false });
            }}
            onChange={(event) =>
              updateBrowsing({
                query: event.currentTarget.value,
                suggestionsClosed: false,
                focusId: TITLE_QUERY_FOCUS_ID,
              })
            }
            onKeyDown={(event) => {
              if (event.key === "ArrowDown" && panelOpen) {
                event.preventDefault();
                root.current
                  ?.querySelector<HTMLElement>(
                    "#bh-matches button, #bh-matches a",
                  )
                  ?.focus();
              }
              if (event.key === "Escape") {
                event.preventDefault();
                event.stopPropagation();
                updateBrowsing({ suggestionsClosed: true });
              }
            }}
            aria-controls="bh-matches"
            aria-expanded={panelOpen}
          />
          <button
            type="submit"
            id="bh-search-submit"
            aria-label="Run search"
            className={styles.searchSubmit}
          >
            <FontAwesomeIcon icon={faArrowRight} />
          </button>
        </form>
        {panelOpen && (browsing.query.trim() || fallbackAvailable) && (
          <div
            id="bh-matches"
            className={styles.candidates}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                event.preventDefault();
                document.getElementById(TITLE_QUERY_INPUT_ID)?.focus();
                updateBrowsing({ suggestionsClosed: true });
              }
              if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                const controls = Array.from(
                  event.currentTarget.querySelectorAll<HTMLElement>(
                    "button:not(:disabled), a",
                  ),
                );
                const index = controls.indexOf(
                  document.activeElement as HTMLElement,
                );
                if (index >= 0) {
                  event.preventDefault();
                  controls[
                    (index +
                      (event.key === "ArrowDown" ? 1 : -1) +
                      controls.length) %
                      controls.length
                  ]?.focus();
                }
              }
            }}
          >
            {contextualMatches.map(
              (source) =>
                source.matches.length > 0 && (
                  <section key={source.id} aria-label={source.label}>
                    <Text className={styles.groupLabel}>{source.label}</Text>
                    {source.matches.map((match) => (
                      <button
                        type="button"
                        className={styles.quickMatch}
                        key={match.id}
                        onClick={() => {
                          updateBrowsing({ suggestionsClosed: true });
                          match.select();
                        }}
                      >
                        <span>{match.title}</span>
                        <small>{match.detail}</small>
                      </button>
                    ))}
                  </section>
                ),
            )}
            {localMatches.length > 0 && (
              <section aria-label="In your library">
                <Text className={styles.groupLabel}>In your library</Text>
                {localMatches.map((item) => {
                  const path = `/${item.sonarrSeriesId != null ? "series" : "movies"}/${item.id}`;
                  return (
                    <Link
                      className={styles.quickMatch}
                      key={path}
                      to={path}
                      onClick={() =>
                        updateBrowsing({ suggestionsClosed: true })
                      }
                    >
                      <span>
                        {item.title}
                        {item.year ? ` (${item.year})` : ""}
                      </span>
                      <small>
                        {item.sonarrSeriesId != null ? "Series" : "Movie"}
                        {item.arr_instance_id != null
                          ? ` · Library ${item.arr_instance_id}`
                          : ""}
                      </small>
                    </Link>
                  );
                })}
              </section>
            )}
            {searching && library.isError && (
              <Text size="sm" role="status">
                Library search is unavailable. Try again.
              </Text>
            )}
            {pages.length > 0 && (
              <section aria-label="Pages">
                <Text className={styles.groupLabel}>Pages</Text>
                {pages.map((page) => (
                  <Link
                    key={`${page.path}:${page.title}`}
                    to={page.path}
                    className={styles.quickMatch}
                    onClick={() => updateBrowsing({ suggestionsClosed: true })}
                  >
                    {page.title}
                  </Link>
                ))}
              </section>
            )}
            <div
              aria-live="polite"
              role="status"
              className={styles.searchStatus}
            >
              {status.settingsLoading && <Text>Loading Discover setup.</Text>}
              {status.settingsError && (
                <Alert color="yellow">
                  Discover setup could not be loaded. Reload this page to retry.
                  You can open IMDb subtitle search below.
                </Alert>
              )}
              {!status.settingsLoading &&
                !status.settingsError &&
                !status.configured && (
                  <Alert color="yellow">
                    Set up TMDB in the Subtitle Hub to browse movies beyond your
                    library. IMDb subtitle search and your local library remain
                    available.
                  </Alert>
                )}
              {source &&
                ["authentication_failed", "unavailable"].includes(
                  source.status,
                ) && <Alert color="yellow">{source.message}</Alert>}
              {(status.isError || results.isError) && (
                <Alert color="yellow">
                  {shows ? "Show" : "Movie"} metadata could not be loaded. Retry
                  the search.
                </Alert>
              )}
              {Boolean(normalized) &&
                status.configured &&
                normalized === debounced &&
                results.isFetching && (
                  <Text>Searching {shows ? "show" : "movie"} titles.</Text>
                )}
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
              {!hasOtherMatches &&
                !fetching &&
                !results.isError &&
                (!additional ||
                  (!additional.isError &&
                    additional.data &&
                    ["available", "cached"].includes(
                      additional.data.status,
                    ))) &&
                results.data &&
                ["available", "cached"].includes(results.data.status) &&
                normalized === debounced &&
                !items.length &&
                !browsing.suggestionsClosed &&
                Boolean(normalized) && (
                  <Text>No catalog titles matched this search.</Text>
                )}
            </div>
            {searching && additional && (
              <div aria-live="polite" role="status">
                {additional.isFetching && <Text>Searching show titles.</Text>}
                {additional.isError && (
                  <Alert color="yellow">
                    Show metadata could not be loaded. Retry the search.
                  </Alert>
                )}
                {additional.data && (
                  <>
                    {additional.data.status === "cached" && (
                      <Text size="sm">
                        Shows: cached {additional.data.source.toUpperCase()}{" "}
                        metadata
                        {additional.data.service_status
                          ? ", catalog service is temporarily unavailable"
                          : ""}
                        .
                        {additional.data.fetched_at && (
                          <>
                            {" "}
                            <time dateTime={additional.data.fetched_at}>
                              {new Date(
                                additional.data.fetched_at,
                              ).toLocaleString()}
                            </time>
                          </>
                        )}
                      </Text>
                    )}
                    {([
                      "unavailable",
                      "authentication_failed",
                      "unconfigured",
                    ].includes(additional.data.status) ||
                      additional.data.failure_reason) && (
                      <Alert color="yellow">
                        Shows:{" "}
                        {additional.data.message ||
                          "Catalog metadata is unavailable. Results cover only the available sources."}
                      </Alert>
                    )}
                    {additional.data.source !== "tmdb" &&
                      additional.data.primary &&
                      (["unavailable", "authentication_failed"].includes(
                        additional.data.primary.status,
                      ) ||
                        additional.data.primary.service_status ===
                          "unavailable") && (
                        <Alert color="yellow">
                          Shows:{" "}
                          {additional.data.primary.message ||
                            "TMDB is temporarily unavailable. Cached catalog records may be shown."}
                        </Alert>
                      )}
                    {additional.data.fallback &&
                      (["unavailable", "authentication_failed"].includes(
                        additional.data.fallback.status,
                      ) ||
                        additional.data.fallback.failure_reason) && (
                        <Alert color="yellow">
                          Shows: {additional.data.fallback.message}
                        </Alert>
                      )}
                  </>
                )}
              </div>
            )}
            {(status.isError ||
              results.isError ||
              additional?.isError ||
              source?.status === "unavailable" ||
              additional?.data?.status === "unavailable") && (
              <Button
                variant="light"
                mb="md"
                onClick={() => {
                  void status.refetch();
                  if (normalized) void results.refetch();
                  if (normalized && additional) void additional.refetch();
                }}
              >
                Retry metadata
              </Button>
            )}
            {source?.status === "authentication_failed" && (
              <Anchor
                id="discover-metadata-setup"
                c="var(--discover-link)"
                component={Link}
                to="/subtitle-hub?tab=my-providers#metadata"
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
                  <Button
                    variant="subtle"
                    onClick={() => void results.refetch()}
                  >
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
            {(results.data?.local_truncated ||
              results.data?.truncated ||
              additional?.data?.local_truncated ||
              additional?.data?.truncated) && (
              <Text size="sm" mb="md">
                Showing a limited set of matches. Narrow the title to see other
                candidates.
              </Text>
            )}
            {fallbackAvailable && !hasOtherMatches && (
              <Group gap="xs" mb="md">
                <Button
                  id="discover-manual-imdb"
                  variant="subtle"
                  onClick={() =>
                    openManualSearch("title", "discover-manual-imdb")
                  }
                >
                  Search subtitles by IMDb ID
                </Button>
                <Button
                  id="discover-manual-release"
                  variant="subtle"
                  onClick={() =>
                    openManualSearch("release", "discover-manual-release")
                  }
                >
                  Search providers by release name
                </Button>
              </Group>
            )}
            <Stack
              gap={0}
              aria-label={
                allMedia
                  ? "Movie and show candidates"
                  : shows
                    ? "Show candidates"
                    : "Movie candidates"
              }
            >
              {items.length > 0 && (
                <Text className={styles.groupLabel}>Catalog</Text>
              )}
              {items.slice(0, 12).map((movie) => (
                <div key={movie.source_id} className={styles.candidate}>
                  <span aria-hidden="true">
                    <MediaPoster
                      key={movie.poster_url}
                      src={movie.poster_url}
                    />
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
                          suggestionsClosed: true,
                          selectedId: movie.id,
                          selectedSource: movie.source,
                          selectedType: movie.media_type,
                          selectedSeason: reopen
                            ? browsing.selectedSeason
                            : null,
                          selectedEpisode: reopen
                            ? browsing.selectedEpisode
                            : null,
                          adoptedSourceId: reopen ? movie.source_id : null,
                          identityLoaded: reopen,
                          adoptedMovieId: reopen ? movie.id : null,
                        });
                        void navigate(
                          discoverTitlePath(
                            movie.source,
                            movie.media_type,
                            movie.id,
                            reopen ? browsing.selectedSeason : null,
                            reopen ? browsing.selectedEpisode : null,
                          ),
                        );
                        if (!reopen) {
                          updateDraft({
                            mediaType:
                              movie.media_type === "show" ? "episode" : "movie",
                            showId:
                              movie.media_type === "show" &&
                              movie.source === "tmdb"
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
                    </Text>
                  </Stack>
                </div>
              ))}
            </Stack>
          </div>
        )}
      </div>
    </section>
  );
}
