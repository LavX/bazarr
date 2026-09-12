import { FormEvent, useEffect, useLayoutEffect, useRef } from "react";
import {
  Link,
  useLocation,
  useNavigate,
  useNavigationType,
} from "react-router";
import {
  Alert,
  Anchor,
  Button,
  Checkbox,
  Group,
  SegmentedControl,
  Stack,
  Text,
  TextInput,
  Title,
  useComputedColorScheme,
} from "@mantine/core";
import { faMagnifyingGlass } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useSystemSettings } from "@/apis/hooks";
import { useDiscoverMetadata } from "@/apis/hooks/discover";
import { useLanguageProfiles, useLanguages } from "@/apis/hooks/languages";
import { useProviderHubProviders } from "@/apis/hooks/providerHub";
import { useDiscover } from "@/contexts/Discover";
import {
  discoverPageKey,
  recentEpisodeMismatch,
  searchSelection,
} from "@/contexts/discoverState";
import DigitalReleases from "./DigitalReleases";
import DiscoverSelect from "./DiscoverSelect";
import { readableTime } from "./feedText";
import LibraryActivity from "./LibraryActivity";
import LocalCopyPicker from "./LocalCopyPicker";
import ProviderCoverage from "./ProviderCoverage";
import RecentEpisodes from "./RecentEpisodes";
import SearchProgress from "./SearchProgress";
import SubtitleResults from "./SubtitleResults";
import TitleDetails, { TitleEpisodePicker, TitleNotes } from "./TitleDetails";
import Trending from "./Trending";
import styles from "./Discover.module.scss";

/** Where a reader can install or enable a catalog provider. */
const HUB_ROUTE = "/subtitle-hub?tab=marketplace";

function listNames(names: string[]): string {
  if (names.length <= 1) return names.join("");
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

export default function Discover() {
  const {
    state,
    rememberPage,
    restorePage,
    updateDraft,
    updateBrowsing,
    seedLanguage,
    findSubtitles,
    cancelPending,
  } = useDiscover();
  const { draft, snapshot } = state;
  const optionsOpen = state.browsing.optionsOpen;
  const previousTitle = useRef(state.browsing.selectedId);
  useEffect(() => {
    if (previousTitle.current !== state.browsing.selectedId) {
      previousTitle.current = state.browsing.selectedId;
      updateBrowsing({ optionsOpen: false });
    }
  }, [state.browsing.selectedId, updateBrowsing]);
  const colorScheme = useComputedColorScheme("light");
  const location = useLocation();
  const navigate = useNavigate();
  const adoptedRoute = useRef<string | null>(null);
  const route = new URLSearchParams(location.search);
  const linkedShow = route.get("show");
  const linkedMovie = route.get("movie");
  const linkedSeason = route.get("season");
  const linkedEpisode = route.get("episode");
  const source = route.get("source") ?? "tmdb";
  const kind = linkedShow === null ? "movie" : "show";
  const linkedId = linkedShow ?? linkedMovie;
  const invalidEpisodeLink =
    (linkedId !== null || linkedSeason !== null || linkedEpisode !== null) &&
    (linkedId === null ||
      (linkedShow !== null && linkedMovie !== null) ||
      !["tmdb", "omdb", "local"].includes(source) ||
      ["show", "movie", "source", "season", "episode"].some(
        (key) => route.getAll(key).length > 1,
      ) ||
      (source !== "omdb"
        ? !/^[1-9]\d{0,12}$/.test(linkedId)
        : !linkedId || linkedId.length > 256) ||
      (linkedSeason !== null &&
        (kind !== "show" || !/^(0|[1-9]\d{0,3})$/.test(linkedSeason))) ||
      (linkedEpisode !== null &&
        (!/^[1-9]\d{0,3}$/.test(linkedEpisode) || linkedSeason === null)));
  const visitedPage = useRef<{
    key: string;
    state: typeof state | null;
  } | null>(null);
  const routeMode = route.get("mode") === "release" ? "release" : "title";
  const matchesRoute =
    !invalidEpisodeLink &&
    (linkedId !== null
      ? String(state.browsing.selectedId) === linkedId &&
        state.browsing.selectedSource === source &&
        state.browsing.selectedType === kind &&
        state.browsing.selectedSeason ===
          (linkedSeason === null ? null : Number(linkedSeason)) &&
        state.browsing.selectedEpisode ===
          (linkedEpisode === null ? null : Number(linkedEpisode)) &&
        (state.draft.mode ?? "title") === routeMode
      : state.browsing.selectedId === null &&
        state.browsing.manualSearch ===
          ["title", "release"].includes(route.get("mode") ?? "") &&
        (state.draft.mode ?? "title") === routeMode);
  useLayoutEffect(() => {
    const previous = visitedPage.current;
    if (previous?.key !== location.key) {
      if (previous?.state) rememberPage(previous.key, previous.state);
      // Selection handlers may update context before navigation commits. Never
      // file that incoming selection under the outgoing browser history entry.
      visitedPage.current = { key: location.key, state: null };
      if (restorePage(location.key)) {
        adoptedRoute.current = location.key;
        return;
      }
    }
    if (matchesRoute) visitedPage.current = { key: location.key, state };
  }, [location.key, state, matchesRoute, rememberPage, restorePage]);
  useLayoutEffect(
    () => () => {
      const previous = visitedPage.current;
      if (previous?.state) rememberPage(previous.key, previous.state);
    },
    [rememberPage],
  );
  useEffect(() => {
    if (adoptedRoute.current === location.key) return;
    adoptedRoute.current = location.key;
    const params = new URLSearchParams(location.search);
    const manual = params.get("mode");
    if (invalidEpisodeLink || linkedId === null) {
      const manualSearch =
        !invalidEpisodeLink && (manual === "title" || manual === "release");
      cancelPending();
      updateBrowsing({
        selectedId: null,
        selectedSeason: null,
        selectedEpisode: null,
        manualSearch,
        identityLoaded: false,
        adoptedSourceId: null,
        adoptedMovieId: null,
        retainedTitle: null,
        recentContext: null,
        releaseContext: null,
        optionsOpen: false,
        suggestionsClosed: true,
      });
      updateDraft({
        mode: manualSearch && manual === "release" ? "release" : "title",
        ...(manualSearch
          ? {}
          : {
              mediaType: "movie",
              imdbId: "",
              title: undefined,
              year: undefined,
              query: "",
              showId: undefined,
              showTvdbId: undefined,
              episodeIdentity: undefined,
              manualEntry: false,
              manualConfirmed: false,
              season: "",
              episode: "",
            }),
      });
      return;
    }
    const id = source === "omdb" ? linkedId : Number(linkedId);
    const season = linkedSeason === null ? null : Number(linkedSeason);
    const episode = linkedEpisode === null ? null : Number(linkedEpisode);
    const sameTitle =
      state.browsing.selectedSource === source &&
      state.browsing.selectedType === kind &&
      state.browsing.selectedId === id;
    const same =
      sameTitle &&
      state.browsing.selectedSeason === season &&
      state.browsing.selectedEpisode === episode;
    // Click handlers have already selected their title. History and pasted URLs
    // adopt the URL instead, without creating another browser history entry.
    const mode = manual === "release" ? "release" : "title";
    if (
      same &&
      !state.browsing.manualSearch &&
      (state.draft.mode ?? "title") === mode
    )
      return;
    cancelPending();
    updateBrowsing({
      selectedId: id,
      selectedSource: source as "tmdb" | "omdb" | "local",
      selectedType: kind,
      selectedSeason: season,
      selectedEpisode: episode,
      mediaFilter: kind,
      suggestionsClosed: true,
      optionsOpen: false,
      ...(sameTitle
        ? {}
        : {
            identityLoaded: false,
            adoptedSourceId: null,
            adoptedMovieId: null,
            retainedTitle: null,
          }),
    });
    updateDraft({
      mode,
      mediaType: kind === "show" ? "episode" : "movie",
      showId: kind === "show" && source === "tmdb" ? Number(id) : undefined,
      imdbId: sameTitle ? state.draft.imdbId : "",
      title: sameTitle ? state.draft.title : undefined,
      year: sameTitle ? state.draft.year : undefined,
      showTvdbId: sameTitle ? state.draft.showTvdbId : undefined,
      episodeIdentity: undefined,
      manualConfirmed: false,
      manualEntry: source !== "tmdb",
      season: "",
      episode: "",
    });
  }, [
    location.key,
    location.search,
    linkedId,
    linkedSeason,
    linkedEpisode,
    source,
    kind,
    invalidEpisodeLink,
    state.browsing,
    state.draft,
    updateBrowsing,
    updateDraft,
    cancelPending,
  ]);
  const changeMode = (mode: "title" | "release") => {
    updateDraft({ mode });
    const params = new URLSearchParams(location.search);
    if (mode === "title" && state.browsing.selectedId !== null)
      params.delete("mode");
    else params.set("mode", mode);
    void navigate(`/discover?${params}`);
  };
  const navigationType = useNavigationType();
  const pageKey = discoverPageKey(state);
  const browsingPage =
    state.browsing.selectedId === null &&
    !state.browsing.manualSearch &&
    draft.mode !== "release";
  const restoredPage = useRef<string | null>(null);
  useEffect(() => {
    if (!matchesRoute) return;
    if (browsingPage) {
      restoredPage.current = null;
      return;
    }
    if (restoredPage.current === pageKey) return;
    if (navigationType === "PUSH" && state.browsing.selectedId !== null) {
      const frame = window.requestAnimationFrame(() => {
        restoredPage.current = pageKey;
        window.scrollTo({ top: 0, behavior: "instant" });
      });
      return () => window.cancelAnimationFrame(frame);
    }
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
    if (state.browsing.manualSearch && state.browsing.selectedId === null) {
      restoredPage.current = pageKey;
      if (!retrievalForm.current?.contains(document.activeElement))
        document
          .getElementById(
            draft.mode === "release"
              ? "discover-release-query"
              : "discover-imdb-id",
          )
          ?.focus();
    }
  }, [
    matchesRoute,
    browsingPage,
    pageKey,
    navigationType,
    state.browsing.pagePosition,
    state.browsing.manualSearch,
    state.browsing.selectedId,
    draft.mode,
  ]);

  // The homepage carries no retrieval controls: TitleSearch restores ids
  // inside its own section and each feed restores its own card prefix. The
  // retrieval form and its results render only once a title is selected, so
  // there is no homepage retrieval return to restore here. The selected-title
  // path above restores through browsing.pagePosition.
  const retrievalForm = useRef<HTMLFormElement>(null);
  const retrievalResults = useRef<HTMLDivElement>(null);

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
  // The first visit starts with a language rather than an inert button. The
  // seed comes only from the reader's own language profiles, never from the
  // browser locale, the film region or anything else, and only into an empty
  // field: a remembered or chosen language is never touched. The state marks
  // it as seeded so the field says where it came from.
  const profiles = useLanguageProfiles();
  const settings = useSystemSettings();
  const seedAttempted = useRef(false);
  useEffect(() => {
    if (seedAttempted.current || draft.language) return;
    if (!languages.data || !profiles.data) return;
    if (!settings.data && !settings.isError) return;
    seedAttempted.current = true;
    const general = settings.data?.general;
    const preferred =
      draft.mediaType === "episode"
        ? general?.serie_default_enabled
          ? general.serie_default_profile
          : undefined
        : general?.movie_default_enabled
          ? general.movie_default_profile
          : undefined;
    const profile =
      profiles.data.find((row) => row.profileId === preferred) ??
      profiles.data[0];
    const code2 = profile?.items?.[0]?.language;
    const match = code2
      ? languages.data.find((language) => language.code2 === code2)
      : undefined;
    if (match) seedLanguage(match.code3);
  }, [
    draft.language,
    draft.mediaType,
    languages.data,
    profiles.data,
    settings.data,
    settings.isError,
    seedLanguage,
  ]);
  const searching = state.status === "searching";
  const searched = snapshot !== null || state.status === "failed";
  const recentMismatch = recentEpisodeMismatch(state);
  const canSearch = searchSelection(draft) !== null && !recentMismatch;
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!searching && canSearch) void findSubtitles(searched);
  };
  const detailsPage = !browsingPage && !releaseMode;
  const providers = snapshot?.coverage.providers ?? [];
  // Knowable before a search: Discover searches trusted catalog providers
  // only, so an install whose enabled providers are all built-ins has nothing
  // Discover can search. That is said here, next to the action, rather than
  // discovered by burning a search. The rule mirrors the server's own:
  // enabled, installed from the Hub, trusted, active, not awaiting restart.
  const hub = useProviderHubProviders();
  const enabledProviders = settings.data?.general.enabled_providers ?? [];
  const hubInstall = (name: string) =>
    (hub.data ?? []).find((item) => item.provider_id === name);
  const catalogReady = enabledProviders.filter((name) => {
    const install = hubInstall(name);
    return Boolean(
      install &&
      install.trusted &&
      install.state === "active" &&
      !install.pending_restart,
    );
  });
  // Why each enabled provider is not searchable, from what the Hub actually
  // says. Absence from the Hub is the only evidence that a provider is not a
  // catalog install; an install that is present but untrusted, inactive or
  // awaiting a restart is none of those things, and saying it is built in
  // would be false at exactly the moment a reader returns from installing one.
  // Each reason carries both numbers. Grouping puts several providers under one
  // of them, so a phrase that only reads correctly for a single provider gives
  // a reader with three built-ins "bsplayer, gestdown and yifysubtitles are not
  // a catalog provider", which is the common case rather than the edge one.
  const notSearchable = enabledProviders
    .filter((name) => !catalogReady.includes(name))
    .map((name) => {
      const install = hubInstall(name);
      const label = install?.name?.trim() || name;
      if (!install)
        return {
          label,
          one: "is not a catalog provider",
          many: "are not catalog providers",
        };
      if (install.pending_restart)
        return {
          label,
          one: "is waiting for a restart",
          many: "are waiting for a restart",
        };
      if (!install.trusted)
        return { label, one: "is not trusted", many: "are not trusted" };
      if (install.state !== "active")
        return {
          label,
          one: `is not active (${install.state})`,
          many: `are not active (${install.state})`,
        };
      return {
        label,
        one: "is not available to Discover",
        many: "are not available to Discover",
      };
    });
  const groupedReasons = [
    ...new Set(notSearchable.map((item) => item.one)),
  ].map((one) => {
    const group = notSearchable.filter((item) => item.one === one);
    return { one, many: group[0].many, names: group.map((i) => i.label) };
  });
  const readiness: "unknown" | "none" | "no-catalog" | "ready" =
    !settings.data || !hub.data
      ? "unknown"
      : enabledProviders.length === 0
        ? "none"
        : catalogReady.length === 0
          ? "no-catalog"
          : "ready";
  // After a search: nothing searched at all is a different fact from
  // providers that searched and failed, and it gets its own sentence.
  const nothingSearched =
    snapshot?.status === "failed" &&
    providers.length > 0 &&
    providers.every(
      (provider) =>
        provider.status === "skipped" || provider.status === "setup_required",
    );
  const skippedBuiltIns = providers
    .filter((provider) => provider.reason === "not_catalog_provider")
    .map((provider) => provider.provider);
  const empty =
    state.status === "complete" &&
    snapshot?.status === "complete" &&
    snapshot.results.length === 0;
  const noProviders =
    snapshot !== null && snapshot.coverage.configured_count === 0;

  const languageField = (
    <DiscoverSelect
      id="discover-subtitle-language"
      label="Subtitle language"
      placeholder="Choose a language"
      searchable
      value={draft.language}
      options={languageOptions}
      inputWrapperOrder={["label", "input", "description", "error"]}
      description={
        state.languageSeeded && draft.language
          ? "Preselected from your language profile. Change it here at any time."
          : "Your language choice is remembered for Discover. Library profiles stay unchanged."
      }
      onChange={(value) => updateDraft({ language: value })}
    />
  );
  const hubLink = (
    <Anchor
      component={Link}
      to={HUB_ROUTE}
      className={styles.hubLink}
      onClick={() =>
        updateBrowsing({
          returnTarget: location.pathname + location.search + location.hash,
        })
      }
    >
      Open the Subtitle Hub
    </Anchor>
  );
  const readinessNotice =
    !releaseMode && readiness === "none" ? (
      <Text className={styles.readiness} role="status">
        Add a subtitle provider to start searching.
      </Text>
    ) : !releaseMode && readiness === "no-catalog" ? (
      <Alert color="yellow" className={styles.readiness} role="status">
        Discover cannot search any of your enabled providers, because it
        searches installed, trusted catalog providers only.{" "}
        {groupedReasons.map(({ one, many, names }, index) => (
          <span key={one}>
            {index > 0 ? " " : ""}
            {listNames(names)} {names.length === 1 ? one : many}.
          </span>
        ))}{" "}
        {hubLink}
      </Alert>
    ) : null;
  const needsProviderSetup =
    !releaseMode && (readiness === "none" || readiness === "no-catalog");
  const submitButton = needsProviderSetup ? (
    <Button component={Link} to={HUB_ROUTE} variant="filled">
      Set up providers
    </Button>
  ) : (
    <Button
      type="submit"
      variant="filled"
      disabled={!canSearch || searching}
      loading={searching}
      leftSection={<FontAwesomeIcon icon={faMagnifyingGlass} />}
    >
      {searching
        ? "Finding subtitles"
        : searched
          ? "Search again"
          : "Find subtitles"}
    </Button>
  );
  return (
    <section
      id="bazarr-discover-home"
      className={styles.discover}
      aria-labelledby="discover-title"
      data-theme={colorScheme === "light" ? "day" : "night"}
      data-density="cinematic"
      data-page={browsingPage ? "home" : "title"}
    >
      <Title order={1} id="discover-title" className={styles.visuallyHidden}>
        Discover
      </Title>

      <div
        className={styles.homepageLayout}
        data-page={browsingPage ? "home" : "title"}
      >
        <div className={styles.homepageContent}>
          {invalidEpisodeLink && (
            <Alert color="red">
              This title or episode link is incomplete or invalid. Choose a
              title and episode.
            </Alert>
          )}
          {releaseMode ? (
            <Stack gap="sm" mb={24}>
              {!metadata.configured && (
                <Alert color="yellow">
                  Set up TMDB in the Subtitle Hub to explore global titles.
                  Release-name search remains available.
                </Alert>
              )}
              {metadata.data?.status === "authentication_failed" && (
                <Alert color="yellow">
                  TMDB rejected the key Discover is using. Check it in the
                  Subtitle Hub.
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
                c="var(--discover-link)"
                component={Link}
                to="/subtitle-hub?tab=my-providers#metadata"
                className={styles.settingsLink}
              >
                Subtitle Hub
              </Anchor>
            </Stack>
          ) : browsingPage ? (
            <>
              <div id="bh-browse">
                <LibraryActivity />
                <Trending />
                {state.browsing.trendingFilter !== "movie" && (
                  <RecentEpisodes />
                )}
                {state.browsing.trendingFilter !== "series" && (
                  <DigitalReleases />
                )}
              </div>
            </>
          ) : state.browsing.selectedId !== null ? (
            <div id="bh-detail">
              {state.browsing.recentContext && (
                <Stack gap="xs" mb="md" aria-label="Selected recent episode">
                  <Text size="sm">
                    {state.browsing.recentContext.item.show_title} · S
                    {state.browsing.recentContext.item.season} E
                    {state.browsing.recentContext.item.episode}:{" "}
                    {state.browsing.recentContext.item.title} · Original air
                    date:{" "}
                    <time
                      className={styles.dateValue}
                      dateTime={state.browsing.recentContext.item.air_date}
                    >
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
              <TitleDetails
                onOptions={() => updateBrowsing({ optionsOpen: !optionsOpen })}
                optionsOpen={optionsOpen}
              />
            </div>
          ) : null}

          {!browsingPage && state.browsing.selectedId === null && (
            <Button
              variant="subtle"
              className={styles.detailBack}
              onClick={() => {
                cancelPending();
                updateDraft({ mode: "title" });
                updateBrowsing({
                  manualSearch: false,
                  suggestionsClosed: false,
                });
                void navigate("/discover");
              }}
            >
              Back to Discover
            </Button>
          )}

          {releaseMode && (
            <Group className={styles.modeControls} justify="space-between">
              <Text fw={600}>Advanced release-name search</Text>
              <Button
                type="button"
                variant="subtle"
                onClick={() => changeMode("title")}
              >
                {state.browsing.selectedId === null
                  ? "Search subtitles by IMDb ID"
                  : "Return to identified title"}
              </Button>
            </Group>
          )}

          {!browsingPage && (
            <form
              ref={retrievalForm}
              onSubmit={submit}
              className={styles.searchForm}
              data-page={detailsPage ? "title" : "release"}
            >
              {releaseMode && (
                <Text size="sm" mb="lg" maw="70ch">
                  Title and episode identity and timing compatibility are
                  unverified. This query is not linked to a library copy.
                  Episode filenames need one explicit season and episode, for
                  example Show.S02E03.
                </Text>
              )}
              {detailsPage && (
                <section className={styles.retrievalPanel}>
                  <Title order={2} className={styles.retrievalTitle}>
                    Find the subtitles you need
                  </Title>
                  <TitleEpisodePicker showOptions={optionsOpen} />
                  <div className={styles.primaryRow}>
                    {languageField}
                    {submitButton}
                  </div>
                  {readinessNotice}
                </section>
              )}
              <div
                className={
                  releaseMode ? styles.releaseFields : styles.targetFields
                }
                data-secondary={detailsPage ? "true" : undefined}
                style={
                  !releaseMode &&
                  !optionsOpen &&
                  !state.browsing.manualSearch &&
                  !(state.browsing.identityLoaded && !draft.imdbId) &&
                  !(draft.mediaType === "episode" && draft.manualEntry)
                    ? { display: "none" }
                    : undefined
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
                    <div className={styles.field}>
                      <Text
                        component="span"
                        className={styles.fieldLabel}
                        id="discover-media-type-label"
                      >
                        Media type
                      </Text>
                      <SegmentedControl
                        id="discover-media-type"
                        aria-labelledby="discover-media-type-label"
                        classNames={{
                          root: styles.segmented,
                          label: styles.segmentedLabel,
                          indicator: styles.segmentedIndicator,
                        }}
                        value={draft.mediaType}
                        data={[
                          { value: "movie", label: "Movie" },
                          { value: "episode", label: "Episode" },
                        ]}
                        onChange={(value) => {
                          updateDraft({
                            mediaType:
                              value === "episode" ? "episode" : "movie",
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
                            manualSearch: true,
                            releaseContext: null,
                            recentContext: null,
                            identityLoaded: false,
                            adoptedMovieId: null,
                            adoptedSourceId: null,
                            suggestionsClosed: true,
                          });
                        }}
                      />
                    </div>
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
                          manualSearch: true,
                          releaseContext: null,
                          recentContext: null,
                          identityLoaded: false,
                          adoptedMovieId: null,
                          adoptedSourceId: null,
                          suggestionsClosed: true,
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
                {!detailsPage && languageField}
              </div>
              {!releaseMode &&
                draft.mediaType === "episode" &&
                (draft.manualEntry || state.browsing.manualSearch) && (
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
                    c="var(--discover-link)"
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
              {detailsPage ? null : (
                <Group
                  className={styles.submitRow}
                  justify="space-between"
                  align="center"
                >
                  <Text size="sm">
                    Search mode: release name. Results have unverified identity
                    and compatibility.
                  </Text>
                  {submitButton}
                </Group>
              )}
              {/*
              A target reached by browsing gets copy matching in the detail
              view. The picker derives its own target and renders nothing until
              that target is exact. There is one instance of it, here, rendered
              for the selected-title page, so a chosen copy survives within the
              detail. Release-name search has no identified target, so it has
              nothing to match against.
            */}
              {!releaseMode && <LocalCopyPicker />}
            </form>
          )}
          {detailsPage && <TitleNotes />}
          {detailsPage && optionsOpen && (
            <div className={styles.modeSwitch}>
              <Button
                type="button"
                variant="subtle"
                onClick={() => changeMode("release")}
              >
                Search providers by release name
              </Button>
            </div>
          )}

          {!browsingPage && (
            <div role="status" aria-live="polite" className={styles.status}>
              {searching && <SearchProgress progress={state.searchProgress} />}
              {state.error && <Alert color="red">{state.error}</Alert>}
              {noProviders && (
                <Alert color="yellow" title="Set up a subtitle provider">
                  No subtitle provider is enabled. Discover searches trusted
                  catalog providers from the Subtitle Hub; install or enable
                  one, then return to this search and try again. {hubLink}
                </Alert>
              )}
              {!searching &&
                !state.error &&
                snapshot?.status === "failed" &&
                !noProviders &&
                (nothingSearched ? (
                  <Alert color="yellow">
                    No provider searched this title.{" "}
                    {/* The pre-search notice already names the built-ins when
                      the condition was knowable; say it once. */}
                    {skippedBuiltIns.length > 0 &&
                      readiness !== "no-catalog" && (
                        <>
                          {listNames(skippedBuiltIns)}{" "}
                          {skippedBuiltIns.length === 1
                            ? "is a built-in provider"
                            : "are built-in providers"}
                          , and Discover searches trusted catalog providers
                          only.{" "}
                        </>
                      )}
                    Provider details are below. {hubLink}
                  </Alert>
                ) : (
                  <Alert color="yellow">
                    No provider completed this search. Review provider details
                    below before retrying.
                  </Alert>
                ))}
              {!searching && empty && (
                <Text>
                  {releaseMode
                    ? "No results returned for this unverified release query."
                    : "No subtitles matched this title and language. All searched providers completed."}
                </Text>
              )}
              {!searching && snapshot?.cache_status === "stale" && (
                <Text size="sm">
                  Some previous results are retained. Their original checked
                  times are shown.
                </Text>
              )}
            </div>
          )}

          {!browsingPage && (
            <div ref={retrievalResults}>
              {snapshot && (
                <Stack
                  gap="lg"
                  id="bh-subtitle-results"
                  className={styles.resultsPanel}
                >
                  <Title
                    order={2}
                    aria-label="Subtitle results"
                    className={styles.resultsTitle}
                    title={`Checked ${readableTime(snapshot.checked_at)}`}
                  >
                    {snapshot.context.mode === "release"
                      ? snapshot.context.query
                      : (snapshot.context.title ?? snapshot.context.imdb_id)}
                  </Title>
                  <Text size="sm" c="dimmed" className={styles.resultsNote}>
                    {languages.data?.find(
                      (language) =>
                        language.code2 === snapshot.context.language ||
                        language.code3 === snapshot.context.language,
                    )?.name ?? snapshot.context.language}
                    {` · ${snapshot.results.length} subtitle ${snapshot.results.length === 1 ? "result" : "results"}`}
                    {snapshot.context.mode === "release"
                      ? " · Unverified release query"
                      : ""}
                    {snapshot.cache_status === "cached"
                      ? " · Cached search"
                      : ""}
                  </Text>
                  <Text className={styles.visuallyHidden}>
                    Checked{" "}
                    <time dateTime={snapshot.checked_at}>
                      {readableTime(snapshot.checked_at)}
                    </time>
                  </Text>
                  {snapshot.context.episode_identity && (
                    <Text size="sm" className={styles.resultsNote}>
                      Source episode: S
                      {snapshot.context.episode_identity.season} E
                      {snapshot.context.episode_identity.episode}:{" "}
                      {snapshot.context.episode_identity.title}
                      {snapshot.context.episode_identity.air_date
                        ? ` · Original air date: ${snapshot.context.episode_identity.air_date}`
                        : " · Original air date unavailable"}
                    </Text>
                  )}
                  {snapshot.context.media_type === "episode" && (
                    <Text size="sm" className={styles.resultsNote}>
                      {snapshot.context.manual_confirmed
                        ? "Manual episode recovery. Source numbering and identity are unverified."
                        : "Target numbering: TVDB default order. Catalog ordering and timing compatibility may differ."}
                    </Text>
                  )}
                  <SubtitleResults snapshot={snapshot} />
                  <ProviderCoverage
                    providers={providers}
                    context={snapshot.context}
                  />
                </Stack>
              )}
            </div>
          )}
        </div>
        <div
          id="bh-announcement"
          className={styles.visuallyHidden}
          aria-live="polite"
        />
      </div>
    </section>
  );
}
