import { FC, useState } from "react";
import { Paper, Radio, Stack, Text, Title } from "@mantine/core";
import type { MediaServerKind } from "@/apis/raw/mediaServers";
import { kindName } from "@/pages/Settings/MediaServers/kinds";
import InstanceServerForm from "./mediaServer/InstanceServerForm";
import PlexServerForm from "./mediaServer/PlexServerForm";
import StepActions from "./mediaServer/StepActions";
import type { WizardStepProps } from "./types";

interface KindChoice {
  value: MediaServerKind;
  description: string;
}

// The same four kinds, in the same order, as the Connections tabs.
const CHOICES: KindChoice[] = [
  {
    value: "plex",
    description:
      "Sign in with your Plex account and pick the server to refresh.",
  },
  {
    value: "jellyfin",
    description: "Connect with the server URL and an API key.",
  },
  {
    value: "emby",
    description:
      "Connect with the server URL and an API key, then map your media folders.",
  },
  {
    value: "silo",
    description:
      "Connect with the server URL and an API key, then map your media folders to a library.",
  },
];

/**
 * Optional onboarding step for an external media server. A kind picker first,
 * then the fields that kind actually needs: a tab pair invited filling in two
 * servers at once on a step most people skip, and it hid the two kinds whose
 * connection is not a typed host and key at all.
 *
 * Every kind stays optional and Continue with nothing filled in writes
 * nothing, because Bazarr+ finds and downloads subtitles with no media server
 * at all. Skipping is the shell's control.
 */
const MediaServerStep: FC<WizardStepProps> = ({ onNext, onBack }) => {
  const [kind, setKind] = useState<MediaServerKind | null>(null);

  return (
    <Stack gap="lg">
      <Stack gap="xs">
        <Title order={2}>Media servers</Title>
        <Text c="dimmed">
          Optionally connect a media server so Bazarr refreshes it after it
          downloads subtitles. Bazarr finds and downloads subtitles with no
          media server at all, so you can set one up later in Settings,
          Connections.
        </Text>
      </Stack>

      <Radio.Group
        value={kind ?? ""}
        onChange={(value) => setKind(value as MediaServerKind)}
        aria-label="Which media server do you have?"
      >
        <Stack gap="sm">
          {CHOICES.map((choice) => (
            <Paper key={choice.value} withBorder p="md" radius="md">
              <Radio
                value={choice.value}
                label={kindName(choice.value)}
                description={choice.description}
              />
            </Paper>
          ))}
        </Stack>
      </Radio.Group>

      {kind === "plex" ? (
        <PlexServerForm onNext={onNext} onBack={onBack} />
      ) : kind ? (
        // Keyed by kind: without it React reuses the instance across a switch
        // and the prefilled name, the typed URL and the credential would all
        // carry over from the kind the user just left.
        <InstanceServerForm
          key={kind}
          kind={kind}
          onNext={onNext}
          onBack={onBack}
        />
      ) : (
        <StepActions
          onNext={onNext}
          onBack={onBack}
          continueLabel="Continue without a server"
        />
      )}
    </Stack>
  );
};

export default MediaServerStep;
