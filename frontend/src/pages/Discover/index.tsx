import { FormEvent, useEffect, useRef } from "react";
import { Link, useLocation } from "react-router";
import {
  Alert,
  Anchor,
  Button,
  Checkbox,
  Group,
  NativeSelect,
  Stack,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { faMagnifyingGlass } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useDiscoverMetadata } from "@/apis/hooks/discover";
import { useLanguages } from "@/apis/hooks/languages";
import { useDiscover } from "@/contexts/Discover";
import {
  discoverPageKey,
  recentEpisodeMismatch,
  searchSelection,
} from "@/contexts/discoverState";
import type { DiscoverProviderOutcome } from "@/types/discover";
import DigitalReleases from "./DigitalReleases";
import MetadataAttribution from "./MetadataAttribution";
import RecentEpisodes from "./RecentEpisodes";
import SubtitleResults from "./SubtitleResults";
import TitleDetails from "./TitleDetails";
import TitleSearch from "./TitleSearch";
import Trending from "./Trending";
import styles from "./Discover.module.scss";

const outcomeLabels: Record<DiscoverProviderOutcome["status"], string> = {
  success: "Search complete",
  empty: "No matches",
  unverified:
    "No results returned for this unverified query. Provider query support could not be confirmed.",
  authentication_required: "Sign in to this provider in provider settings",
  setup_required: "Provider setup required",
  cooldown: "Provider is cooling down",
  unreachable: "Provider could not be reached",
  timeout: "Provider search timed out",
  error: "Provider search failed",
  skipped: "Provider could not search this target",
  saturated: "Search capacity is busy. Try again shortly",
};

const skipLabels: Record<string, string> = {
  unsupported_media: "This provider does not support this media type",
  excluded_language: "This language is excluded in provider settings",
  unsupported_language: "This provider does not support this language",
  requires_file: "This provider needs a video file",
  not_catalog_provider:
    "Enable a trusted catalog provider in provider settings",
  provider_unavailable: "Install or enable this provider in provider settings",
};

export default function Discover() {
  const { state, updateDraft, updateBrowsing, findSubtitles } = useDiscover();
  const { draft, snapshot } = state;
  const location = useLocation();
  const adoptedRoute = useRef<string | null>(null);
  const route = new URLSearchParams(location.search);
  const linkedShow = route.get("show");
  const linkedSeason = route.get("season");
  const linkedEpisode = route.get("episode");
  const invalidEpisodeLink =
    (linkedShow !== null || linkedSeason !== null || linkedEpisode !== null) &&
    (linkedShow === null ||
      ["show", "season", "episode"].some(
        (key) => route.getAll(key).length > 1,
      ) ||
      !/^[1-9]\d{0,12}$/.test(linkedShow) ||
      (linkedSeason !== null && !/^(0|[1-9]\d{0,3})$/.test(linkedSeason)) ||
      (linkedEpisode !== null &&
        (!/^[1-9]\d{0,3}$/.test(linkedEpisode) || linkedSeason === null)));
  useEffect(() => {
    if (adoptedRoute.current === location.search) return;
    adoptedRoute.current = location.search;
    if (invalidEpisodeLink) {
      updateDraft({
        mediaType: "episode",
        imdbId: "",
        episodeIdentity: undefined,
        manualConfirmed: false,
        season: "",
        episode: "",
      });
      updateBrowsing({ selectedId: null });
      return;
    }
    if (linkedShow === null) return;
    const showId = Number(linkedShow);
    const season = linkedSeason === null ? null : Number(linkedSeason);
    const episode = linkedEpisode === null ? null : Number(linkedEpisode);
    const same =
      state.browsing.selectedSource === "tmdb" &&
      state.browsing.selectedType === "show" &&
      state.browsing.selectedId === showId &&
      state.browsing.selectedSeason === season &&
      state.browsing.selectedEpisode === episode;
    updateBrowsing({
      selectedId: showId,
      selectedSource: "tmdb",
      selectedType: "show",
      selectedSeason: season,
      selectedEpisode: episode,
      mediaFilter: "show",
      returnTarget: location.pathname + location.search + location.hash,
    });
    const sameShow =
      state.browsing.selectedSource === "tmdb" &&
      state.browsing.selectedType === "show" &&
      state.browsing.selectedId === showId;
    if (!same)
      updateDraft({
        mediaType: "episode",
        showId,
        imdbId: sameShow ? state.draft.imdbId : "",
        title: sameShow ? state.draft.title : undefined,
        year: sameShow ? state.draft.year : undefined,
        episodeIdentity: undefined,
        manualConfirmed: false,
        manualEntry: false,
        season: "",
        episode: "",
      });
  }, [
    location.search,
    location.pathname,
    location.hash,
    linkedShow,
    linkedSeason,
    linkedEpisode,
    invalidEpisodeLink,
    state.browsing,
    state.draft,
    updateBrowsing,
    updateDraft,
  ]);
  const pageKey = discoverPageKey(state);
  const browsingPage =
    state.browsing.selectedId === null && draft.mode !== "release";
  const restoredPage = useRef<string | null>(null);
  useEffect(() => {
    if (browsingPage) {
      restoredPage.current = null;
      return;
    }
    if (restoredPage.current === pageKey) return;
    const saved = state.browsing.pagePosition;
    const control =
      saved?.target === pageKey ? document.getElementById(saved.focusId) : null;
    if (control && saved) {
      // Native history restores its offset after the route commit. Reconcile
      // the continuation at the next paint, and cancel if this page leaves.
      const frame = window.requestAnimationFrame(() => {
        restoredPage.current = pageKey;
        control.focus({ preventScroll: true });
        window.scrollTo({ top: saved.scrollY, behavior: "instant" });
        const bounds = control.getBoundingClientRect();
        const shellBottom = Math.max(
          0,
          document
            .querySelector(".mantine-AppShell-header")
            ?.getBoundingClientRect().bottom ?? 0,
        );
        // A navigation link can scroll away from the continuation control before
        // capture. Keep a visible saved position, otherwise reveal the control.
        const adjustment =
          bounds.top < shellBottom
            ? bounds.top - shellBottom - 8
            : bounds.bottom > window.innerHeight
              ? bounds.bottom - window.innerHeight + 8
              : 0;
        if (adjustment)
          window.scrollTo({
            top: Math.max(0, window.scrollY + adjustment),
            behavior: "instant",
          });
      });
      return () => window.cancelAnimationFrame(frame);
    }
  }, [browsingPage, pageKey, state.browsing.pagePosition]);
  useEffect(() => {
    const active = document.activeElement;
    let focusId =
      active instanceof HTMLElement &&
      active.closest("[aria-labelledby=discover-title]") &&
      active.id
        ? active.id
        : undefined;
    const rememberPosition = () => {
      if (!focusId) return;
      const position = { focusId, scrollY: window.scrollY };
      if (browsingPage) updateBrowsing(position);
      else updateBrowsing({ pagePosition: { target: pageKey, ...position } });
    };
    const rememberFocus = (event: FocusEvent) => {
      const target = event.target;
      if (
        target instanceof HTMLElement &&
        target.closest("[aria-labelledby=discover-title]") &&
        target.id
      ) {
        focusId = target.id;
        rememberPosition();
      }
    };
    document.addEventListener("focusin", rememberFocus);
    window.addEventListener("scroll", rememberPosition, { passive: true });
    // Capture the actual position before navigation handlers run, even when
    // the browser has not delivered its pending scroll event yet.
    const rememberNavigation = (event: MouseEvent) => {
      if (event.target instanceof Element && event.target.closest("a[href]"))
        rememberPosition();
    };
    document.addEventListener("click", rememberNavigation, true);
    return () => {
      document.removeEventListener("focusin", rememberFocus);
      window.removeEventListener("scroll", rememberPosition);
      document.removeEventListener("click", rememberNavigation, true);
    };
  }, [updateBrowsing, pageKey, browsingPage]);
  const languages = useLanguages();
  const releaseMode = draft.mode === "release";
  const metadata = useDiscoverMetadata("status", undefined, releaseMode);
  const languageOptions = (languages.data ?? []).map((language) => ({
    value: language.code3,
    label: language.name,
  }));
  if (
    draft.language &&
    !languageOptions.some((option) => option.value === draft.language)
  ) {
    languageOptions.push({ value: draft.language, label: draft.language });
  }
  const searching = state.status === "searching";
  const searched = snapshot !== null || state.status === "failed";
  const recentMismatch = recentEpisodeMismatch(state);
  const canSearch = searchSelection(draft) !== null && !recentMismatch;
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!searching && canSearch) void findSubtitles(searched);
  };
  const providers = snapshot?.coverage.providers ?? [];
  const empty =
    state.status === "complete" &&
    snapshot?.status === "complete" &&
    snapshot.results.length === 0;
  const noProviders =
    snapshot !== null && snapshot.coverage.configured_count === 0;

  return (
    <section className={styles.discover} aria-labelledby="discover-title">
      <header className={styles.header}>
        <div>
          <Title order={1} id="discover-title">
            Discover
          </Title>
          <Text className={styles.intro}>Browse films and series.</Text>
        </div>
        <Anchor
          c="light-dark(var(--mantine-color-brand-7), var(--mantine-color-brand-4))"
          component={Link}
          to="/subtitle-hub"
          className={styles.settingsLink}
        >
          Provider settings
        </Anchor>
      </header>

      <div className={styles.homepageLayout}>
        <div className={styles.homepageContent}>
          {invalidEpisodeLink && (
            <Alert color="red">
              This episode link is incomplete or invalid. Choose a show and
              episode.
            </Alert>
          )}
          {releaseMode ? (
            <Stack gap="sm" mb={24}>
              {!metadata.configured && (
                <Alert color="yellow">
                  Set up TMDB in Discover settings to explore global titles.
                  Release-name search remains available.
                </Alert>
              )}
              {metadata.data?.status === "authentication_failed" && (
                <Alert color="yellow">
                  TMDB rejected the saved token. Replace it in Discover
                  settings.
                </Alert>
              )}
              {(metadata.isError ||
                metadata.data?.status === "unavailable") && (
                <Alert color="yellow">
                  Global metadata is temporarily unavailable. Release-name
                  search remains available.
                </Alert>
              )}
              <Anchor
                c="light-dark(var(--mantine-color-brand-7), var(--mantine-color-brand-4))"
                component={Link}
                to="/settings/discover"
                className={styles.settingsLink}
              >
                Discover settings
              </Anchor>
            </Stack>
          ) : state.browsing.selectedId === null ? (
            <>
              <div className={styles.homepageSearch}>
                <TitleSearch />
              </div>
              <Trending />
              <DigitalReleases />
              <RecentEpisodes />
            </>
          ) : (
            <>
              {state.browsing.releaseContext && (
                <Text size="sm" mb="md" aria-label="Selected digital release">
                  Digital · {state.browsing.releaseContext.region} ·{" "}
                  <time dateTime={state.browsing.releaseContext.release_date}>
                    {state.browsing.releaseContext.release_date}
                  </time>{" "}
                  · TMDB regional release record
                  {state.browsing.releaseContext.fetched_at && (
                    <>
                      {" "}
                      · Checked{" "}
                      {new Date(
                        state.browsing.releaseContext.fetched_at,
                      ).toLocaleString()}
                    </>
                  )}
                </Text>
              )}
              {state.browsing.recentContext && (
                <Stack gap="xs" mb="md" aria-label="Selected recent episode">
                  <Text size="sm">
                    {state.browsing.recentContext.item.show_title} · S
                    {state.browsing.recentContext.item.season} E
                    {state.browsing.recentContext.item.episode}:{" "}
                    {state.browsing.recentContext.item.title} · Original air
                    date:{" "}
                    <time dateTime={state.browsing.recentContext.item.air_date}>
                      {state.browsing.recentContext.item.air_date}
                    </time>{" "}
                    · TMDB season record
                  </Text>
                  {recentMismatch && draft.episodeIdentity && (
                    <Alert color="yellow">
                      The feed record and current episode details need
                      reconciliation. Review the exact source episode below
                      before continuing.
                      {draft.episodeIdentity.identity_status !== "conflict" && (
                        <Button
                          id="discover-recent-reconcile"
                          variant="subtle"
                          onClick={() =>
                            updateBrowsing({ recentContext: null })
                          }
                        >
                          Use current episode details
                        </Button>
                      )}
                    </Alert>
                  )}
                </Stack>
              )}
              <TitleDetails />
            </>
          )}

          <Group className={styles.modeControls} justify="space-between">
            <Text fw={600}>
              {releaseMode
                ? "Advanced release-name search"
                : "Identified title search"}
            </Text>
            <Button
              type="button"
              variant="subtle"
              onClick={() =>
                updateDraft({ mode: releaseMode ? "title" : "release" })
              }
            >
              {releaseMode
                ? "Return to identified title"
                : "Search providers by release name"}
            </Button>
          </Group>

          <form onSubmit={submit} className={styles.searchForm}>
            {releaseMode && (
              <Text size="sm" mb="lg" maw="70ch">
                Title and episode identity and timing compatibility are
                unverified. This query is not linked to a library copy. Episode
                filenames need one explicit season and episode, for example
                Show.S02E03.
              </Text>
            )}
            <div
              className={
                releaseMode ? styles.releaseFields : styles.targetFields
              }
            >
              {releaseMode ? (
                <TextInput
                  id="discover-release-query"
                  label="Release name"
                  placeholder="Example.Movie.2024.1080p.WEB-DL"
                  description="Send this release name to subtitle providers only when you choose Find subtitles."
                  value={draft.query ?? ""}
                  autoComplete="off"
                  spellCheck={false}
                  maxLength={500}
                  onChange={(event) =>
                    updateDraft({ query: event.currentTarget.value })
                  }
                />
              ) : (
                <>
                  <NativeSelect
                    id="discover-media-type"
                    label="Media type"
                    value={draft.mediaType}
                    data={[
                      { value: "movie", label: "Movie" },
                      { value: "episode", label: "Episode" },
                    ]}
                    onChange={(event) => {
                      updateDraft({
                        mediaType:
                          event.currentTarget.value === "episode"
                            ? "episode"
                            : "movie",
                        title: undefined,
                        year: undefined,
                        showId: undefined,
                        episodeIdentity: undefined,
                        manualConfirmed: false,
                        manualEntry: true,
                        season: "",
                        episode: "",
                      });
                      updateBrowsing({
                        selectedId: null,
                        identityLoaded: false,
                        adoptedMovieId: null,
                        adoptedSourceId: null,
                        suggestionsClosed: true,
                        focusId: "discover-media-type",
                        scrollY: window.scrollY,
                      });
                    }}
                  />
                  <TextInput
                    id="discover-imdb-id"
                    label="IMDb ID"
                    placeholder="tt0133093"
                    value={draft.imdbId}
                    autoComplete="off"
                    spellCheck={false}
                    maxLength={30}
                    description={
                      draft.mediaType === "episode"
                        ? "Use the series IMDb ID."
                        : "Use the film IMDb ID."
                    }
                    onChange={(event) => {
                      updateDraft({
                        imdbId: event.currentTarget.value,
                        title: undefined,
                        year: undefined,
                        showId: undefined,
                        episodeIdentity: undefined,
                        manualConfirmed: false,
                        manualEntry: true,
                        season: "",
                        episode: "",
                      });
                      updateBrowsing({
                        selectedId: null,
                        identityLoaded: false,
                        adoptedMovieId: null,
                        adoptedSourceId: null,
                        suggestionsClosed: true,
                        focusId: "discover-imdb-id",
                        scrollY: window.scrollY,
                      });
                    }}
                  />
                  {draft.mediaType === "episode" && (
                    <div className={styles.episodeFields}>
                      <TextInput
                        label="Season"
                        inputMode="numeric"
                        value={draft.season}
                        readOnly={
                          Boolean(draft.episodeIdentity) && !draft.manualEntry
                        }
                        maxLength={4}
                        onChange={(event) =>
                          updateDraft({
                            season: event.currentTarget.value,
                            manualConfirmed: false,
                          })
                        }
                      />
                      <TextInput
                        label="Episode"
                        inputMode="numeric"
                        value={draft.episode}
                        readOnly={
                          Boolean(draft.episodeIdentity) && !draft.manualEntry
                        }
                        maxLength={4}
                        onChange={(event) =>
                          updateDraft({
                            episode: event.currentTarget.value,
                            manualConfirmed: false,
                          })
                        }
                      />
                    </div>
                  )}
                </>
              )}
              <NativeSelect
                id="discover-subtitle-language"
                label="Subtitle language"
                value={draft.language}
                data={[
                  { value: "", label: "Choose a language" },
                  ...languageOptions,
                ]}
                onChange={(event) =>
                  updateDraft({ language: event.currentTarget.value })
                }
              />
            </div>
            {!releaseMode &&
              draft.mediaType === "episode" &&
              (!draft.episodeIdentity || draft.manualEntry) && (
                <Checkbox
                  mt="md"
                  className={styles.manualConfirmation}
                  label="I confirm this series IMDb ID and the manual season and episode numbers"
                  description="Manual recovery. Source numbering and episode identity remain unverified."
                  checked={Boolean(draft.manualConfirmed)}
                  disabled={
                    !/^tt\d{7,10}$/.test(draft.imdbId) ||
                    !/^\d{1,4}$/.test(draft.season) ||
                    !/^[1-9]\d{0,3}$/.test(draft.episode)
                  }
                  onChange={(event) =>
                    updateDraft({
                      manualConfirmed: event.currentTarget.checked,
                    })
                  }
                />
              )}
            {languages.isError && (
              <Text c="red" size="sm" mt="sm">
                The language list could not be loaded.{" "}
                <Anchor
                  c="light-dark(var(--mantine-color-brand-7), var(--mantine-color-brand-4))"
                  component="button"
                  type="button"
                  onClick={() => void languages.refetch()}
                >
                  Retry languages
                </Anchor>
              </Text>
            )}
            {!state.storageAvailable && (
              <Text size="sm" mt="sm">
                Your language is remembered for this visit. Browser storage is
                unavailable.
              </Text>
            )}
            <Group
              className={styles.submitRow}
              justify="space-between"
              align="center"
            >
              <Text size="sm">
                {releaseMode
                  ? "Search mode: release name. Results have unverified identity and compatibility."
                  : "Search by title identity. Choose an exact episode for series."}
              </Text>
              <Button
                type="submit"
                disabled={!canSearch || searching}
                loading={searching}
                leftSection={<FontAwesomeIcon icon={faMagnifyingGlass} />}
              >
                {searching
                  ? "Finding subtitles"
                  : searched
                    ? "Refresh subtitles"
                    : "Find subtitles"}
              </Button>
            </Group>
          </form>

          <div role="status" aria-live="polite" className={styles.status}>
            {state.status === "unsearched" && (
              <Text>
                {releaseMode
                  ? "Enter a release name and choose one subtitle language, then choose Find subtitles."
                  : "Select a title and a subtitle language, then choose Find subtitles."}
              </Text>
            )}
            {searching && (
              <Text>
                Searching providers.{" "}
                {snapshot?.results.length
                  ? "Previous results remain available below."
                  : "Results will appear here."}
              </Text>
            )}
            {state.error && <Alert color="red">{state.error}</Alert>}
            {noProviders && (
              <Alert color="yellow" title="Set up a subtitle provider">
                Enable a provider in{" "}
                <Anchor component={Link} to="/subtitle-hub">
                  Provider settings
                </Anchor>
                , then return to this search and try again.
              </Alert>
            )}
            {!searching && !state.error && snapshot?.status === "partial" && (
              <Alert color="yellow">
                Some providers could not complete this search. Available results
                are shown below.
              </Alert>
            )}
            {!searching &&
              !state.error &&
              snapshot?.status === "failed" &&
              !noProviders && (
                <Alert color="yellow">
                  No provider completed this search. Review provider details
                  below before retrying.
                </Alert>
              )}
            {!searching && empty && (
              <Text>
                {releaseMode
                  ? "No results returned for this unverified release query."
                  : "No subtitles matched this title and language. All searched providers completed."}
              </Text>
            )}
            {!searching && snapshot?.cache_status === "stale" && (
              <Text size="sm">
                Some previous results are retained. Their original checked times
                are shown.
              </Text>
            )}
          </div>

          {snapshot && (
            <Stack gap="lg">
              <Group justify="space-between" align="baseline">
                <Title order={2}>
                  Subtitle results
                  {snapshot.results.length
                    ? ` (${snapshot.results.length})`
                    : ""}
                </Title>
                <Text size="sm" c="dimmed">
                  {snapshot.cache_status === "cached"
                    ? "Cached search"
                    : "Checked"}{" "}
                  <time dateTime={snapshot.checked_at}>
                    {new Date(snapshot.checked_at).toLocaleString()}
                  </time>
                </Text>
              </Group>
              <Text size="sm" c="dimmed">
                {snapshot.context.mode === "release"
                  ? snapshot.context.query
                  : (snapshot.context.title ?? snapshot.context.imdb_id)}
                {snapshot.context.media_type === "episode"
                  ? `, season ${snapshot.context.season}, episode ${snapshot.context.episode}`
                  : ""}
                {` · ${snapshot.context.language} · ${snapshot.context.mode === "release" ? "Unverified release query" : "Title matching"}`}
              </Text>
              {snapshot.context.episode_identity && (
                <Text size="sm">
                  Source episode: S{snapshot.context.episode_identity.season} E
                  {snapshot.context.episode_identity.episode}:{" "}
                  {snapshot.context.episode_identity.title}
                  {snapshot.context.episode_identity.air_date
                    ? ` · Original air date: ${snapshot.context.episode_identity.air_date}`
                    : " · Original air date unavailable"}
                </Text>
              )}
              {snapshot.context.media_type === "episode" && (
                <Text size="sm">
                  {snapshot.context.manual_confirmed
                    ? "Manual episode recovery. Source numbering and identity are unverified."
                    : "Target numbering: TVDB default order. Catalog ordering and timing compatibility may differ."}
                </Text>
              )}
              <SubtitleResults snapshot={snapshot} />
              {providers.length > 0 && (
                <section aria-labelledby="discover-coverage">
                  <Title order={3} id="discover-coverage" size="h4">
                    Provider coverage
                  </Title>
                  <ul className={styles.providerList}>
                    {providers.map((provider) => (
                      <li key={provider.provider}>
                        <div>
                          <Text fw={600}>{provider.provider}</Text>
                          <Text size="sm">
                            {skipLabels[provider.reason ?? ""] ??
                              outcomeLabels[provider.status]}
                          </Text>
                          {provider.retry_at && (
                            <Text size="sm" c="dimmed">
                              Retry after{" "}
                              <time dateTime={provider.retry_at}>
                                {new Date(provider.retry_at).toLocaleString()}
                              </time>
                            </Text>
                          )}
                        </div>
                        <Text size="sm" c="dimmed">
                          {provider.result_count}{" "}
                          {provider.result_count === 1 ? "result" : "results"}
                        </Text>
                      </li>
                    ))}
                  </ul>
                </section>
              )}
            </Stack>
          )}
          <MetadataAttribution />
        </div>
        <aside
          className={styles.localSummary}
          aria-labelledby="discover-local-title"
        >
          <Title order={2} id="discover-local-title">
            Your Bazarr+
          </Title>
          <Text size="sm">
            Library automation is optional. Manage your local work separately
            from global discovery.
          </Text>
          <Anchor
            c="light-dark(var(--mantine-color-brand-8), var(--mantine-color-brand-4))"
            component={Link}
            to="/system/tasks"
          >
            Activity
          </Anchor>
          <Anchor
            c="light-dark(var(--mantine-color-brand-8), var(--mantine-color-brand-4))"
            component={Link}
            to="/settings/connections"
          >
            Library connections
          </Anchor>
          <Anchor
            c="light-dark(var(--mantine-color-brand-8), var(--mantine-color-brand-4))"
            component={Link}
            to="/setup"
          >
            Optional setup guide
          </Anchor>
        </aside>
      </div>
    </section>
  );
}
