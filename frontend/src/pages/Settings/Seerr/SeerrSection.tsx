import { FunctionComponent, useCallback, useState } from "react";
import { Alert, Button, Text as MantineText } from "@mantine/core";
import { useSeerrTestConnectionMutation } from "@/apis/hooks/seerr";
import {
  Check,
  CollapseBox,
  Password,
  Section,
  Text,
} from "@/pages/Settings/components";
import { seerrEnabledKey } from "@/pages/Settings/keys";
import { useSettingValue } from "@/pages/Settings/utilities/hooks";
import type { SeerrTestResult } from "@/types/seerr";

const SeerrTestButton: FunctionComponent = () => {
  const [title, setTitle] = useState("Test");
  const [color, setColor] = useState("primary");
  const [warning, setWarning] = useState<string | null>(null);
  const mutation = useSeerrTestConnectionMutation();
  const url = useSettingValue<string>("settings-seerr-url");
  const apikey = useSettingValue<string>("settings-seerr-apikey");
  const verifySsl = useSettingValue<boolean>("settings-seerr-verify_ssl");

  const click = useCallback(() => {
    if (!url || !apikey) {
      setTitle("URL and API key required");
      setColor("danger");
      return;
    }

    setTitle("Testing...");
    setColor("primary");
    setWarning(null);

    mutation.mutate(
      { url, apikey, verifySsl: verifySsl ?? true },
      {
        onSuccess: (data: SeerrTestResult) => {
          if (data.success) {
            const name = data.application_title || "Seerr";
            // `||`, not `??`: the backend normalises a missing display name to
            // an empty string, which would otherwise label the button "as"
            // with nothing after it. Joining the parts, rather than trimming a
            // template, keeps a server with no version from leaving a doubled
            // space in the middle.
            const actor = data.acting_user?.display_name || "owner";
            setTitle(
              [name, data.version, "as", actor].filter(Boolean).join(" "),
            );
            setColor("success");

            if (
              data.acting_user &&
              !(
                data.acting_user.can_request_movie &&
                data.acting_user.can_request_tv
              )
            ) {
              setWarning(
                "This Seerr user cannot request both movies and series. Requests from Bazarr+ will be refused.",
              );
            }
          } else {
            setTitle(
              data.error_code === "configuration"
                ? "URL and API key required"
                : data.error_code === "rejected_key"
                  ? "Seerr rejected the API key"
                  : "Connection failed",
            );
            setColor("danger");
          }
        },
        onError: () => {
          setTitle("Connection failed");
          setColor("danger");
        },
      },
    );
  }, [url, apikey, verifySsl, mutation]);

  return (
    <>
      <Button autoContrast onClick={click} variant={color}>
        {title}
      </Button>
      {warning && (
        <Alert color="yellow" mt="xs">
          {warning}
        </Alert>
      )}
    </>
  );
};

const VerifySslCheck: FunctionComponent = () => {
  // Only show the verify-ssl toggle when the user is using HTTPS. Plain HTTP
  // doesn't go through TLS, so the setting is meaningless and showing it
  // would just invite confusion. The setting itself defaults to `true` and
  // is read by the backend regardless of UI visibility, so hiding the
  // control on HTTP doesn't change behavior - it just keeps the form clean.
  const url = useSettingValue<string>("settings-seerr-url");
  const isHttps = (url ?? "").trim().toLowerCase().startsWith("https://");
  if (!isHttps) return null;
  return (
    <Check
      label="Verify SSL certificate"
      settingKey="settings-seerr-verify_ssl"
    />
  );
};

// Seerr (Jellyseerr/Overseerr) request integration for the Connections page.
// Follows the same shape as every sibling section on this page (Plex,
// Jellyfin, Sonarr, Radarr, the media server variants): an "Enabled" switch
// on its own, then the connection fields collapsed behind it.
const SeerrSection: FunctionComponent = () => {
  return (
    <>
      <Section header="Use Seerr">
        <Check label="Enabled" settingKey={seerrEnabledKey} />
      </Section>

      <CollapseBox settingKey={seerrEnabledKey}>
        <Section header="Connection">
          <Text
            label="Seerr URL"
            settingKey="settings-seerr-url"
            placeholder="http://seerr:5055"
          />
          <Password label="API key" settingKey="settings-seerr-apikey" />
          <VerifySslCheck />
          <Text
            label="Browser URL"
            settingKey="settings-seerr-external_url"
            placeholder="Leave empty to use Seerr's application URL"
            description="Shown to users as the link back to Seerr. Leave empty to use the application URL Seerr reports, falling back to the Seerr URL above."
          />
          <MantineText size="sm" c="dimmed">
            Works with Seerr, Jellyseerr and Overseerr. Requests from Bazarr+
            are made with this API key. Requested as the Seerr owner and
            approved immediately.
          </MantineText>
          <SeerrTestButton />
        </Section>
      </CollapseBox>
    </>
  );
};

export default SeerrSection;
