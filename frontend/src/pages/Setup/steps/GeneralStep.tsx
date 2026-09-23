import { FC } from "react";
import { Button, Group, Select, Switch, TextInput } from "@mantine/core";
import { useSettingsMutation, useSystemSettings } from "@/apis/hooks";
import { folderOptions } from "@/pages/Settings/Subtitles/options";
import StepLayout from "@/pages/Setup/StepLayout";
import { useStepDraft } from "@/pages/Setup/useStepDrafts";
import type { WizardStepProps } from "./types";

// The "alongside media" default keeps subtitles next to the video file; any
// other folder mode needs a companion path, mirroring the Settings page.
const DEFAULT_SUBFOLDER = "current";

function needsCustomFolder(value: string): boolean {
  return value !== "" && value !== DEFAULT_SUBFOLDER;
}

/**
 * Optional onboarding step for a few high-traffic general preferences. It
 * pre-fills from the current settings and only writes the keys the user actually
 * changed, so Continue with no edits just advances.
 */
const GeneralStep: FC<WizardStepProps> = ({ onNext, onBack, stepKey }) => {
  const { data: settings } = useSystemSettings();
  const mutation = useSettingsMutation();

  const general = settings?.general;

  const initialSubfolder = general?.subfolder ?? DEFAULT_SUBFOLDER;
  const initialSubfolderCustom = general?.subfolder_custom ?? "";
  const initialUpgrade = general?.upgrade_subs ?? true;

  // Held by the wizard, so Back and forward keep the edits. Only a field the
  // reader actually changed is stored, so the rest still follows the settings
  // query when it answers after this step first rendered.
  const [draft, patchDraft] = useStepDraft(stepKey, {
    subfolder: initialSubfolder,
    subfolderCustom: initialSubfolderCustom,
    upgradeSubs: initialUpgrade,
  });
  const { subfolder, subfolderCustom, upgradeSubs } = draft;
  const setSubfolder = (value: string) => patchDraft({ subfolder: value });
  const setSubfolderCustom = (value: string) =>
    patchDraft({ subfolderCustom: value });
  const setUpgradeSubs = (value: boolean) => patchDraft({ upgradeSubs: value });

  const showCustomFolder = needsCustomFolder(subfolder);

  const handleContinue = () => {
    const payload: LooseObject = {};

    if (subfolder !== initialSubfolder) {
      payload["settings-general-subfolder"] = subfolder;
    }
    if (showCustomFolder && subfolderCustom !== initialSubfolderCustom) {
      payload["settings-general-subfolder_custom"] = subfolderCustom;
    }
    if (upgradeSubs !== initialUpgrade) {
      payload["settings-general-upgrade_subs"] = upgradeSubs;
    }

    if (Object.keys(payload).length === 0) {
      onNext();
      return;
    }

    mutation.mutate(payload, {
      onSuccess: () => {
        onNext();
      },
    });
  };

  return (
    <StepLayout
      title="General basics"
      description="A few application preferences to round things out. These are optional and can all be changed later in Settings."
      actions={
        <Group justify="space-between">
          <Group gap="sm">
            {onBack && (
              <Button variant="default" onClick={onBack}>
                Back
              </Button>
            )}
          </Group>
          <Button onClick={handleContinue} loading={mutation.isPending}>
            Continue
          </Button>
        </Group>
      }
    >
      <Select
        label="Subtitle Folder"
        description="Where Bazarr stores the subtitles it downloads."
        data={folderOptions}
        value={subfolder}
        onChange={(value) => setSubfolder(value ?? DEFAULT_SUBFOLDER)}
        allowDeselect={false}
      />

      {showCustomFolder && (
        <TextInput
          label="Custom Subtitles Folder"
          description="The path Bazarr should use for the chosen folder mode."
          value={subfolderCustom}
          onChange={(e) => setSubfolderCustom(e.currentTarget.value)}
        />
      )}

      <Switch
        label="Upgrade previously downloaded subtitles"
        description="Periodically look for better matches for subtitles you already have."
        checked={upgradeSubs}
        onChange={(e) => setUpgradeSubs(e.currentTarget.checked)}
      />
    </StepLayout>
  );
};

export default GeneralStep;
