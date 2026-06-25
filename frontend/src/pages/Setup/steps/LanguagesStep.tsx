import { FC, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Group,
  MultiSelect,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import {
  useLanguageProfiles,
  useLanguages,
  useSettingsMutation,
} from "@/apis/hooks";
import { enabledLanguageKey, languageProfileKey } from "@/pages/Settings/keys";
import type { WizardStepProps } from "./types";

/**
 * Onboarding languages step. Bazarr only downloads subtitles for languages that
 * belong to a language profile, so this step does both at once: the user picks
 * the languages they want, and on Continue we build a single "Default" profile
 * with one item per language and assign it as the default for series and movies.
 *
 * Idempotent: if a profile already exists (re-entering the wizard), we show a
 * short note and let Continue advance without rewriting anything.
 */
const LanguagesStep: FC<WizardStepProps> = ({ onNext, onBack }) => {
  const { data: languages } = useLanguages();
  const { data: profiles } = useLanguageProfiles();
  const settings = useSettingsMutation();

  const [selected, setSelected] = useState<string[]>([]);

  const options = useMemo(
    () =>
      (languages ?? []).map((lang) => ({
        value: lang.code2,
        label: lang.name,
      })),
    [languages],
  );

  const alreadyConfigured = (profiles ?? []).length > 0;

  const handleContinue = () => {
    if (alreadyConfigured) {
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

  const canContinue = alreadyConfigured || selected.length > 0;

  return (
    <Stack gap="lg">
      <Stack gap="xs">
        <Title order={2}>Subtitle languages</Title>
        <Text c="dimmed">
          Pick the languages you want subtitles in. Bazarr only searches for
          languages that belong to a profile, so we will turn your selection
          into a default profile that gets applied to every show and movie.
        </Text>
      </Stack>

      {alreadyConfigured ? (
        <Alert color="green" title="Languages already configured">
          A language profile already exists, so we will keep it as-is.
        </Alert>
      ) : (
        <MultiSelect
          label="Languages"
          description="What languages do you want Bazarr to download subtitles in?"
          placeholder="Select one or more languages"
          searchable
          data={options}
          value={selected}
          onChange={setSelected}
        />
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
  );
};

export default LanguagesStep;
