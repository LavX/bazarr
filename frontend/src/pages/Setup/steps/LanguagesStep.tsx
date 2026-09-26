import { FC, useMemo } from "react";
import { Alert, Button, Group, Loader, MultiSelect } from "@mantine/core";
import {
  useLanguageProfiles,
  useLanguages,
  useSettingsMutation,
} from "@/apis/hooks";
import { enabledLanguageKey, languageProfileKey } from "@/pages/Settings/keys";
import StepLayout from "@/pages/Setup/StepLayout";
import { useStepDraft } from "@/pages/Setup/useStepDrafts";
import type { WizardStepProps } from "./types";

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
 * The browser's language as one of Bazarr's code2 values. "en-GB" and "en"
 * both give "en"; the regional ones above keep their own code.
 */
function browserCode2(): string | null {
  const tag = typeof navigator === "undefined" ? "" : navigator.language;
  const lower = tag.toLowerCase();
  const regional = REGIONAL_CODES[lower];
  if (regional !== undefined) {
    return regional;
  }
  const code = lower.split("-")[0] ?? "";
  return code.length === 2 ? code : null;
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
  const settings = useSettingsMutation();

  // Preselected from the browser, so the gate is met by a working default
  // rather than by a disabled button on a step that cannot explain itself. An
  // empty selection is still a selection: once the reader clears the field,
  // the draft holds [] and nothing puts the guess back.
  const preselected = useMemo(() => {
    const code = browserCode2();
    if (code === null) {
      return [];
    }
    return (languages ?? []).some((lang) => lang.code2 === code) ? [code] : [];
  }, [languages]);

  const [draft, patchDraft] = useStepDraft<{ selected: string[] | null }>(
    stepKey,
    { selected: null },
  );
  const selected = draft.selected ?? preselected;
  const setSelected = (next: string[]) => patchDraft({ selected: next });

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
      description="Pick the languages you want subtitles in. Bazarr only searches for languages that belong to a profile, so we will turn your selection into a default profile that gets applied to every show and movie."
      actions={
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
          A language profile already exists, so we will keep it as-is.
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
          onChange={setSelected}
        />
      )}
    </StepLayout>
  );
};

export default LanguagesStep;
