import { FC, useState } from "react";
import {
  Alert,
  Button,
  Group,
  PasswordInput,
  Stack,
  Switch,
  Text,
  TextInput,
} from "@mantine/core";
import { useSettingsMutation } from "@/apis/hooks";
import { useSeerrTestConnectionMutation } from "@/apis/hooks/seerr";
import { seerrEnabledKey } from "@/pages/Settings/keys";
import StepLayout from "@/pages/Setup/StepLayout";
import type { WizardStepProps } from "./types";

/**
 * Optional onboarding step for Seerr (Jellyseerr / Overseerr). Same shape as
 * the arr steps: URL, key, test, then Continue writes the settings keys the
 * Connections page owns and flips use_seerr on. Continue with nothing filled in
 * writes nothing and advances, exactly like the media-server step.
 */
const SeerrStep: FC<WizardStepProps> = ({ onNext, onBack }) => {
  const settings = useSettingsMutation();

  const [url, setUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [verifySsl, setVerifySsl] = useState(true);
  const [saveError, setSaveError] = useState<string | null>(null);

  const trimmedUrl = url.trim();
  const trimmedKey = apiKey.trim();
  const filled = trimmedUrl.length > 0 && trimmedKey.length > 0;
  // verify_ssl only means anything over TLS, mirroring the Connections page.
  const isHttps = trimmedUrl.toLowerCase().startsWith("https://");

  // Editing the URL or the key drops the last verdict, so a green result can
  // never describe a connection that is no longer on screen.
  const test = useSeerrTestConnectionMutation({
    url: trimmedUrl,
    apikey: trimmedKey,
    verifySsl,
  });

  const handleTest = () => {
    test.mutate({ url: trimmedUrl, apikey: trimmedKey, verifySsl });
  };

  const handleContinue = () => {
    if (!filled) {
      onNext();
      return;
    }
    setSaveError(null);
    settings.mutate(
      {
        "settings-seerr-url": trimmedUrl,
        "settings-seerr-apikey": trimmedKey,
        "settings-seerr-verify_ssl": verifySsl,
        [seerrEnabledKey]: true,
      },
      {
        onSuccess: () => {
          onNext();
        },
        onError: () =>
          setSaveError(
            "Bazarr+ could not save the Seerr connection. Try again, or add it later in Settings, Connections.",
          ),
      },
    );
  };

  const result = test.data;
  // A verdict this connection earned. It is dropped whenever the URL or the
  // key changes, so anything other than a green result here means the reader
  // is about to save a connection nobody has proved works.
  const verified = result?.success === true;

  const verdict = (
    <Stack gap="sm">
      {result &&
        (result.success ? (
          <Alert
            color="green"
            title={`Connected to ${result.application_title || "Seerr"}`}
          >
            {result.acting_user?.display_name
              ? `Requesting as ${result.acting_user.display_name}.`
              : "The server responded successfully."}
          </Alert>
        ) : result.error_code === "configuration" ? (
          <Alert color="red" title="URL and API key required">
            Fill in both fields before testing.
          </Alert>
        ) : (
          <Alert color="red" title="Connection failed">
            {result.error_code === "rejected_key"
              ? "Seerr rejected the API key."
              : "Seerr did not respond. Check the URL and the API key."}
          </Alert>
        ))}
      {result?.success &&
        result.acting_user &&
        !(
          result.acting_user.can_request_movie &&
          result.acting_user.can_request_tv
        ) && (
          <Alert color="yellow" title="Limited Seerr permissions">
            This Seerr user cannot request both movies and series. Requests from
            Bazarr+ will be refused. In Seerr, under Settings, Users, give this
            user the Request Movies and Request Series permissions, then test
            again.
          </Alert>
        )}
      {test.isError && (
        <Alert color="red" title="Test failed">
          Could not reach the Bazarr API to run the connection test.
        </Alert>
      )}
      {saveError && (
        <Alert color="red" title="Could not save Seerr">
          {saveError}
        </Alert>
      )}
    </Stack>
  );

  return (
    <StepLayout
      title="Seerr"
      description="Connect Seerr, Jellyseerr or Overseerr and the request button on a title works straight away. Requests are made with this API key, as the Seerr owner, and approved immediately."
      aside={verdict}
      actions={
        <Stack gap={6}>
          {!filled && (
            <Text size="sm" c="dimmed" ta="right">
              Enter a Seerr URL and an API key to test this connection.
            </Text>
          )}
          <Group justify="space-between">
            <Group gap="sm">
              {onBack && (
                <Button variant="default" onClick={onBack}>
                  Back
                </Button>
              )}
            </Group>
            <Group gap="sm">
              <Button
                type="button"
                variant="default"
                loading={test.isPending}
                disabled={!filled}
                onClick={handleTest}
              >
                Test
              </Button>
              {/* The step is optional, so an untested connection is still
                  allowed through. It is not allowed through silently: the
                  label says what is being saved. */}
              <Button onClick={handleContinue} loading={settings.isPending}>
                {!filled
                  ? "Continue without Seerr"
                  : verified
                    ? "Continue"
                    : "Save and continue anyway"}
              </Button>
            </Group>
          </Group>
        </Stack>
      }
    >
      <TextInput
        label="Seerr URL"
        description="Full URL of your Seerr, Jellyseerr or Overseerr server"
        placeholder="http://seerr:5055"
        value={url}
        onChange={(e) => setUrl(e.currentTarget.value)}
      />

      <PasswordInput
        label="API key"
        description="Found in Seerr under Settings, General"
        placeholder="API key"
        autoComplete="new-password"
        value={apiKey}
        onChange={(e) => setApiKey(e.currentTarget.value)}
      />

      {isHttps && (
        <Switch
          label="Verify SSL certificate"
          checked={verifySsl}
          onChange={(e) => setVerifySsl(e.currentTarget.checked)}
        />
      )}
    </StepLayout>
  );
};

export default SeerrStep;
