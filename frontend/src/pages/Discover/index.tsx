import { FormEvent, useEffect, useRef } from "react";
import { Link, useLocation } from "react-router";
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
import type { DiscoverProviderOutcome } from "@/types/discover";
import DigitalReleases from "./DigitalReleases";
import DiscoverSelect from "./DiscoverSelect";
import { readableTime } from "./feedText";
import LocalCopyPicker from "./LocalCopyPicker";
import MetadataAttribution from "./MetadataAttribution";
import RecentEpisodes from "./RecentEpisodes";
import SubtitleResults from "./SubtitleResults";
import SystemSummary from "./SystemSummary";
import TitleDetails, { TitleNotes } from "./TitleDetails";
import TitleSearch from "./TitleSearch";
import Trending from "./Trending";
import styles from "./Discover.module.scss";

/* eslint-disable camelcase -- these keys are the API's own outcome and skip
   codes, so they keep their transport spelling. */
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

// Each reason names what actually happened to that provider. A built-in
// provider that Discover cannot use is enabled and was skipped, so it is not
// told to be enabled; it is told why it was skipped.
const skipLabels: Record<string, string> = {
  unsupported_media: "This provider does not support this media type",
  excluded_language: "This language is excluded in provider settings",
  unsupported_language: "This provider does not support this language",
  requires_file: "This provider needs a video file",
  not_catalog_provider:
    "Built-in provider, skipped. Discover searches trusted catalog providers only.",
  provider_unavailable:
    "Not installed or not loaded, skipped. Check it in the Subtitle Hub.",
};

/** Where a reader can install or enable a catalog provider. */
const HUB_ROUTE = "/subtitle-hub?tab=marketplace";

function listNames(names: string[]): string {
  if (names.length <= 1) return names.join("");
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}
/* eslint-enable camelcase */

export default function Discover() {
  const { state, updateDraft, updateBrowsing, seedLanguage, findSubtitles } =
    useDiscover();
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

  // The selected-title path above restores through browsing.pagePosition. The
  // homepage has no equivalent owner: TitleSearch restores ids inside its own
  // section and each feed restores its own card prefix, so the retrieval form's
  // own controls are restored by nobody, and returning from a local page landed
  // at whatever offset the browser happened to pick. That was measurable on a
  // narrow viewport, where the page is long: 742 against a captured 3038.
  //
  // The saved pair is read once, at mount, so this restores the position the
  // reader left with and cannot be re-triggered by later interaction on the
  // page. Containment in the retrieval form keeps ownership disjoint from the
  // existing owners, so no two of them ever restore the same return.
  const retrievalForm = useRef<HTMLFormElement>(null);
  const retrievalResults = useRef<HTMLDivElement>(null);
  const homeReturn = useRef(
    browsingPage
      ? { focusId: state.browsing.focusId, scrollY: state.browsing.scrollY }
      : null,
  );
  const restoredReturn = useRef(false);
  useEffect(() => {
    const saved = homeReturn.current;
    if (!browsingPage || restoredReturn.current || !saved?.focusId) return;
    // This page owns the retrieval form and the results it renders itself.
    // The title search restores ids inside its own section and each feed
    // restores its own card prefix, so ownership stays disjoint and no return
    // is ever restored twice.
    const control = document.getElementById(saved.focusId);
    const owned =
      retrievalForm.current?.contains(control) ||
      retrievalResults.current?.contains(control);
    if (!control || !owned) return;
    // Same post-commit reconciliation as the selected-title path: native
    // history applies its own offset after the route commit, and this page can
    // leave again before the frame runs.
    const frame = window.requestAnimationFrame(() => {
      restoredReturn.current = true;
      control.focus({ preventScroll: true });
      window.scrollTo({ top: saved.scrollY, behavior: "instant" });
      const bounds = control.getBoundingClientRect();
      const shellBottom = Math.max(
        0,
        document
          .querySelector(".mantine-AppShell-header")
          ?.getBoundingClientRect().bottom ?? 0,
      );
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
  }, [browsingPage]);

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
      description={
        state.languageSeeded && draft.language
          ? "Preselected from your language profile. Change it here at any time."
          : undefined
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
      <Alert color="yellow" className={styles.readiness} role="status">
        No subtitle provider is enabled, so Find subtitles has nothing to
        search. Discover searches trusted catalog providers from the Subtitle
        Hub. {hubLink}
      </Alert>
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
  const submitButton = (
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
          ? "Refresh subtitles"
          : "Find subtitles"}
    </Button>
  );
  return (
    <section className={styles.discover} aria-labelledby="discover-title">
      <header className={styles.header}>
        <div>
          <Title order={1} id="discover-title">
            Discover
          </Title>
          {browsingPage && (
            <Text className={styles.intro}>Browse films and series.</Text>
          )}
        </div>
        <Anchor
          component={Link}
          to="/subtitle-hub"
          className={styles.settingsLink}
        >
          Provider settings
        </Anchor>
      </header>

      <div
        className={styles.homepageLayout}
        data-page={browsingPage ? "home" : "title"}
      >
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
                  TMDB rejected the key Discover is using. Check it in Discover
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
                c="var(--discover-link)"
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
                  <time
                    className={styles.dateValue}
                    dateTime={state.browsing.releaseContext.release_date}
                  >
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
              <TitleDetails />
            </>
          )}

          {!detailsPage && (
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
          )}

          <form
            ref={retrievalForm}
            onSubmit={submit}
            className={styles.searchForm}
          >
            {releaseMode && (
              <Text size="sm" mb="lg" maw="70ch">
                Title and episode identity and timing compatibility are
                unverified. This query is not linked to a library copy. Episode
                filenames need one explicit season and episode, for example
                Show.S02E03.
              </Text>
            )}
            {detailsPage && readinessNotice}
            {detailsPage && (
              <div className={styles.primaryRow}>
                {languageField}
                {submitButton}
              </div>
            )}
            <div
              className={
                releaseMode ? styles.releaseFields : styles.targetFields
              }
              data-secondary={detailsPage ? "true" : undefined}
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
                          mediaType: value === "episode" ? "episode" : "movie",
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
              {!detailsPage && languageField}
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
            {!detailsPage && readinessNotice}
            {detailsPage ? (
              <Text className={styles.primaryHint}>
                Search by title identity. Choose an exact episode for series.
              </Text>
            ) : (
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
                {submitButton}
              </Group>
            )}
            {/*
              A target typed straight into this form is as confirmed as one
              reached by browsing, so it gets the same copy matching. The
              picker derives its own target and renders nothing until that
              target is exact. There is one instance of it, here, rendered for
              the browsing page and the selected-title page alike, so a chosen
              copy survives moving between them. Release-name search has no
              identified target, so it has nothing to match against.
            */}
            {!releaseMode && <LocalCopyPicker />}
          </form>
          {detailsPage && <TitleNotes />}
          {detailsPage && (
            <div className={styles.modeSwitch}>
              <Button
                type="button"
                variant="subtle"
                onClick={() => updateDraft({ mode: "release" })}
              >
                Search providers by release name
              </Button>
            </div>
          )}

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
                No subtitle provider is enabled. Discover searches trusted
                catalog providers from the Subtitle Hub; install or enable one,
                then return to this search and try again. {hubLink}
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
              !noProviders &&
              (nothingSearched ? (
                <Alert color="yellow">
                  No provider searched this title.{" "}
                  {/* The pre-search notice already names the built-ins when
                      the condition was knowable; say it once. */}
                  {skippedBuiltIns.length > 0 && readiness !== "no-catalog" && (
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
                Some previous results are retained. Their original checked times
                are shown.
              </Text>
            )}
          </div>

          <div ref={retrievalResults}>
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
                    <time
                      className={styles.dateValue}
                      dateTime={snapshot.checked_at}
                    >
                      {readableTime(snapshot.checked_at)}
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
                    Source episode: S{snapshot.context.episode_identity.season}{" "}
                    E{snapshot.context.episode_identity.episode}:{" "}
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
          </div>
          <MetadataAttribution />
        </div>
        <SystemSummary />
      </div>
    </section>
  );
}
