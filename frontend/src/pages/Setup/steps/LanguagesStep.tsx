import { FC, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Group,
  Loader,
  MultiSelect,
  Stack,
  VisuallyHidden,
} from "@mantine/core";
import {
  useLanguageProfiles,
  useLanguages,
  useSettingsMutation,
} from "@/apis/hooks";
import { enabledLanguageKey, languageProfileKey } from "@/pages/Settings/keys";
import StepLayout from "@/pages/Setup/StepLayout";
import { useReportStepBusy } from "@/pages/Setup/useStepBusy";
import { useStepDraft } from "@/pages/Setup/useStepDrafts";
import type { WizardStepProps } from "./types";

// How long a selection note waits before it is written (see changeSelection).
const ANNOUNCE_DELAY_MS = 300;

/**
 * Bazarr's own codes for the regional languages it treats as separate.
 *
 * Cutting the tag at the hyphen is wrong for exactly these: pt-BR is "pb"
 * here and pt is European Portuguese, zh-TW and zh-HK are "zt" and zh is
 * Simplified. Preselecting the base code would have built a default profile
 * in a language the reader never picked and did not ask for.
 */
const REGIONAL_CODES: Record<string, string> = {
  "pt-br": "pb",
  "zh-tw": "zt",
  "zh-hk": "zt",
  "zh-mo": "zt",
  "zh-hant": "zt",
};

/**
 * The countries of Latin America a browser can report for Spanish, beside the
 * UN M.49 region es-419 that names them all. Bazarr defines Spanish (Latino)
 * as its own language, "ea", so a tag in this set prefers it and falls back to
 * plain Spanish when the list does not carry it.
 *
 * es-US is left out on purpose. It is Spanish as spoken in the United States,
 * which is not Latin America as a region, and a browser reporting it says
 * nothing about which Spanish the reader's subtitles are in, so it takes plain
 * Spanish like es-ES does.
 */
const LATIN_AMERICAN_REGIONS = new Set([
  "419",
  "ar",
  "bo",
  "cl",
  "co",
  "cr",
  "cu",
  "do",
  "ec",
  "gt",
  "hn",
  "mx",
  "ni",
  "pa",
  "pe",
  "pr",
  "py",
  "sv",
  "uy",
  "ve",
]);

/**
 * The browser's language as Bazarr's code2 values, best guess first. "en-GB"
 * and "en" both give "en"; the regional ones above keep their own code, and
 * Latin American Spanish names "ea" ahead of the "es" it falls back to.
 */
function browserCandidates(): string[] {
  const tag = typeof navigator === "undefined" ? "" : navigator.language;
  const lower = tag.toLowerCase();
  const regional = REGIONAL_CODES[lower];
  if (regional !== undefined) {
    return [regional];
  }
  const [code = "", region = ""] = lower.split("-");
  if (code === "es" && LATIN_AMERICAN_REGIONS.has(region)) {
    return ["ea", "es"];
  }
  return code.length === 2 ? [code] : [];
}

/**
 * Onboarding languages step. Bazarr only downloads subtitles for languages that
 * belong to a language profile, so this step does both at once: the user picks
 * the languages they want, and on Continue we build a single "Default" profile
 * with one item per language and assign it as the default for series and movies.
 *
 * Idempotent: if a profile already exists (re-entering the wizard), we show a
 * short note and let Continue advance without rewriting anything.
 */
const LanguagesStep: FC<WizardStepProps> = ({ onNext, onBack, stepKey }) => {
  const { data: languages, isLoading } = useLanguages();
  const {
    data: profiles,
    isLoading: profilesLoading,
    isError: profilesFailed,
  } = useLanguageProfiles();
  const settings = useSettingsMutation(false, { inlineErrors: true });
  // Leaving while the save is in flight unmounted the step, TanStack dropped
  // its callbacks, and a failure then said nothing anywhere, since the toasts
  // are off for it. The shell holds Back, the skip and Set up later until it
  // lands.
  useReportStepBusy(settings.isPending);
  // The client keeps a restarting or unreachable Bazarr+ out of its toasts,
  // so a write that fails that way said nothing at all unless it is said here.
  const [saveFailed, setSaveFailed] = useState(false);

  // Preselected from the browser, so the gate is met by a working default
  // rather than by a disabled button on a step that cannot explain itself. An
  // empty selection is still a selection: once the reader clears the field,
  // the draft holds [] and nothing puts the guess back.
  const preselected = useMemo(() => {
    const offered = languages ?? [];
    const code = browserCandidates().find((candidate) =>
      offered.some((lang) => lang.code2 === candidate),
    );
    return code === undefined ? [] : [code];
  }, [languages]);

  const [draft, patchDraft] = useStepDraft<{ selected: string[] | null }>(
    stepKey,
    { selected: null },
  );
  const selected = draft.selected ?? preselected;
  const setSelected = (next: string[]) => patchDraft({ selected: next });
  // What the last change to the selection did, said out loud. Measured with
  // Orca, picking a language read only the option under the cursor, and
  // Backspace removed a pill without a word. The note is written a beat after
  // the change: in the same instant, Orca drops it twice over, once as part of
  // the burst of events the pill list makes, and once when the listbox moves
  // its active option and Orca clears its queue of pending announcements.
  const [selectionNote, setSelectionNote] = useState("");
  const noteTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (noteTimer.current !== null) {
        clearTimeout(noteTimer.current);
      }
    },
    [],
  );
  const nameOf = (code: string) =>
    (languages ?? []).find((lang) => lang.code2 === code)?.name ?? code;
  const changeSelection = (next: string[]) => {
    const added = next.filter((code) => !selected.includes(code));
    const removed = selected.filter((code) => !next.includes(code));
    const note = [
      ...added.map((code) => `${nameOf(code)} added`),
      ...removed.map((code) => `${nameOf(code)} removed`),
    ].join(", ");
    if (noteTimer.current !== null) {
      clearTimeout(noteTimer.current);
    }
    setSelectionNote("");
    noteTimer.current = setTimeout(() => {
      noteTimer.current = null;
      setSelectionNote(note);
    }, ANNOUNCE_DELAY_MS);
    setSelected(next);
  };

  const options = useMemo(
    () =>
      (languages ?? []).map((lang) => ({
        value: lang.code2,
        label: lang.name,
      })),
    [languages],
  );

  const alreadyConfigured = (profiles ?? []).length > 0;
  // Whether this install already has profiles is not knowable until that query
  // answers, and "no answer" is not "none": writing the Default profile on a
  // rerun of setup would replace the profiles the reader spent time building.
  // The preselected language makes Continue pressable straight away, so the
  // window between the two queries is now a window someone will press in.
  const profilesUnknown = profilesLoading || profilesFailed;

  const handleContinue = () => {
    if (alreadyConfigured || profilesUnknown) {
      onNext();
      return;
    }

    const nextProfileId =
      (profiles ?? []).reduce((max, p) => Math.max(p.profileId, max), 0) + 1;

    const items: Language.ProfileItem[] = selected.map((code, index) => ({
      id: index + 1,
      language: code,
      audio_exclude: "False",
      audio_only_include: "False",
      forced: "False",
      hi: "False",
      translate_from: null,
    }));

    const profile: Language.Profile = {
      name: "Default",
      profileId: nextProfileId,
      cutoff: null,
      items,
      mustContain: [],
      mustNotContain: [],
      originalFormat: false,
      tag: undefined,
    };

    setSaveFailed(false);
    settings.mutate(
      {
        [enabledLanguageKey]: selected,
        [languageProfileKey]: JSON.stringify([profile]),
        "settings-general-serie_default_enabled": true,
        "settings-general-serie_default_profile": nextProfileId,
        "settings-general-movie_default_enabled": true,
        "settings-general-movie_default_profile": nextProfileId,
      },
      {
        onSuccess: () => {
          onNext();
        },
        onError: () => setSaveFailed(true),
      },
    );
  };

  const canContinue =
    alreadyConfigured ||
    profilesFailed ||
    (!profilesUnknown && selected.length > 0);

  return (
    <StepLayout
      title="Subtitle languages"
      layout="stacked"
      description="Pick the languages you want subtitles in. Bazarr+ only searches for languages that belong to a profile, so your selection becomes the default profile for shows and movies synced from now on. It does not change anything already in your library: to give those the profile, select them in Series or Movies and use Change Profile."
      actions={
        <Stack gap="sm">
          {saveFailed && (
            <Alert color="red" title="Could not save your languages">
              Check that Bazarr+ is still running, then try again.
            </Alert>
          )}
          <Group justify="space-between">
            <Group gap="sm">
              {onBack && (
                <Button variant="default" onClick={onBack}>
                  Back
                </Button>
              )}
            </Group>
            <Button
              onClick={handleContinue}
              loading={settings.isPending}
              disabled={!canContinue}
            >
              Continue
            </Button>
          </Group>
        </Stack>
      }
    >
      {profilesFailed && (
        // Not a dead end and not a silent overwrite: the step advances and
        // writes nothing, which is the only honest answer when we cannot see
        // what is already there.
        <Alert color="yellow" title="Could not check your language profiles">
          Bazarr+ could not read the language profiles this install already has,
          so setup will not change them. You can set your languages in Settings,
          Languages.
        </Alert>
      )}
      {alreadyConfigured ? (
        <Alert color="green" title="Languages already configured">
          A language profile already exists, so setup keeps it as it is.
        </Alert>
      ) : (
        <MultiSelect
          label="Languages"
          description="What languages do you want Bazarr to download subtitles in?"
          // An empty selector that is fully interactive reads as broken, and
          // Continue is disabled beside it with nothing to explain why. While
          // the list is on its way it says so instead.
          placeholder={
            isLoading ? "Loading languages" : "Select one or more languages"
          }
          disabled={isLoading}
          rightSection={isLoading ? <Loader size="xs" /> : undefined}
          searchable
          data={options}
          value={selected}
          onChange={changeSelection}
        />
      )}
      {/* Polite live region rather than role="status", which Orca never reads
          out (see OnboardingWizard.tsx). */}
      <VisuallyHidden
        aria-live="polite"
        aria-atomic="true"
        data-testid="language-announcement"
      >
        {selectionNote}
      </VisuallyHidden>
    </StepLayout>
  );
};

export default LanguagesStep;
