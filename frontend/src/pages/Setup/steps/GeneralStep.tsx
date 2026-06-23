import { FC, useState } from "react";
import {
  Button,
  Group,
  Select,
  Stack,
  Switch,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { useSettingsMutation, useSystemSettings } from "@/apis/hooks";
import { folderOptions } from "@/pages/Settings/Subtitles/options";
import { pageSizeOptions } from "@/pages/Settings/UI/options";
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
const GeneralStep: FC<WizardStepProps> = ({ onNext, onBack }) => {
  const { data: settings } = useSystemSettings();
  const mutation = useSettingsMutation();

  const general = settings?.general;

  const initialSubfolder = general?.subfolder ?? DEFAULT_SUBFOLDER;
  const initialSubfolderCustom = general?.subfolder_custom ?? "";
  const initialUpgrade = general?.upgrade_subs ?? true;
  const initialPageSize = general?.page_size ?? 25;

  const [subfolder, setSubfolder] = useState<string>(initialSubfolder);
  const [subfolderCustom, setSubfolderCustom] = useState<string>(
    initialSubfolderCustom,
  );
  const [upgradeSubs, setUpgradeSubs] = useState<boolean>(initialUpgrade);
  const [pageSize, setPageSize] = useState<number>(initialPageSize);

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
    if (pageSize !== initialPageSize) {
      payload["settings-general-page_size"] = pageSize;
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
    <Stack gap="lg">
      <Stack gap="xs">
        <Title order={2}>General basics</Title>
        <Text c="dimmed">
          A few application preferences to round things out. These are optional
          and can all be changed later in Settings.
        </Text>
      </Stack>

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

      <Select
        label="Page Size"
        description="How many items to show per page in lists."
        data={pageSizeOptions.map((o) => ({
          value: String(o.value),
          label: o.label,
        }))}
        value={String(pageSize)}
        onChange={(value) =>
          setPageSize(value ? Number(value) : initialPageSize)
        }
        allowDeselect={false}
      />

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
    </Stack>
  );
};

export default GeneralStep;
