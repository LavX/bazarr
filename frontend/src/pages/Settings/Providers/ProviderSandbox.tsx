import { FunctionComponent, useEffect } from "react";
import { Alert, Stack } from "@mantine/core";
import { useSystemStatus } from "@/apis/hooks";
import { Check, Message } from "@/pages/Settings/components";

/**
 * The switch for the subtitle provider sandbox, and the notice that says when a
 * provider is running without it, or was not started. The sandbox keeps a
 * provider plugin from reading the configuration or the database and leaves the
 * media library read-only. A kernel with no Landlock runs providers without it;
 * where the host may enforce it but could not confirm it, providers are not
 * started. The notice (worded by the backend, from the same status as System >
 * Status) says which, so nothing here ever reads as protection that is not there.
 */
const ProviderSandboxSetting: FunctionComponent = () => {
  const { data, refetch } = useSystemStatus();
  const providers = data?.sandbox?.providers;
  const notice = providers?.notice ?? null;
  const partial = providers?.state === "partial";
  const pending = data?.sandbox?.pending === true;
  // The host check runs once, in the background, after boot. Opened before it
  // answers, this page asks again until it has, as System > Status does.
  useEffect(() => {
    if (!pending) return;
    const timer = setInterval(() => void refetch(), 3000);
    return () => clearInterval(timer);
  }, [pending, refetch]);
  return (
    <Stack gap="xs">
      <Check
        label="Sandbox subtitle provider plugins"
        settingKey="settings-general-provider_hub_sandbox"
      />
      <Message>
        Subtitle provider plugins cannot read the configuration file or the
        database, and the media library is read-only to them. They keep their
        own data and temporary folders. They are not stopped from changing the
        mode, owner or times of files the Bazarr+ user owns, and, on kernels
        before Landlock ABI 4, nothing stops them calling Bazarr+'s own web API,
        so keep authentication on. Only media inside the root folders of Sonarr,
        Radarr and Sportarr is readable to them. A kernel with no Landlock runs
        providers without the sandbox; where the check cannot confirm it they
        are not started, and the switch turns that off. A notice appears here
        and on System &gt; Status. Takes effect when a provider worker next
        starts. On by default.
      </Message>
      {notice ? (
        <Alert
          color="yellow"
          variant="light"
          title={
            partial
              ? "Sandbox with gaps"
              : providers?.state === "blocked"
                ? "Provider not started"
                : "Running without the sandbox"
          }
        >
          {notice}
        </Alert>
      ) : null}
    </Stack>
  );
};

export default ProviderSandboxSetting;
