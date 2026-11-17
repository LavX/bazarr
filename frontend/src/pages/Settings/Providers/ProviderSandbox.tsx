import { FunctionComponent } from "react";
import { Alert, Stack } from "@mantine/core";
import { useSystemStatus } from "@/apis/hooks";
import { Check, Message } from "@/pages/Settings/components";

/**
 * The switch for the subtitle provider sandbox, and the notice that says when a
 * provider is running without it. The sandbox keeps a provider plugin from
 * reading the configuration or the database and leaves the media library
 * read-only. Where the host cannot apply it the providers still run, and the
 * notice (worded by the backend, from the same status as System > Status) says
 * why, so nothing here ever reads as protection that is not there.
 */
const ProviderSandboxSetting: FunctionComponent = () => {
  const { data } = useSystemStatus();
  const providers = data?.sandbox?.providers;
  const notice = providers?.notice ?? null;
  const partial = providers?.state === "partial";
  return (
    <Stack gap="xs">
      <Check
        label="Sandbox subtitle provider plugins"
        settingKey="settings-general-provider_hub_sandbox"
      />
      <Message>
        Subtitle provider plugins cannot read the configuration file or the
        database, and the media library is read-only to them. They keep their
        own data and temporary folders. Where this host's kernel covers only
        part of it, or none, providers still run and a notice appears here and
        on System &gt; Status. Takes effect when a provider worker next starts.
        On by default.
      </Message>
      {notice ? (
        <Alert
          color="yellow"
          variant="light"
          title={partial ? "Sandbox with gaps" : "Running without the sandbox"}
        >
          {notice}
        </Alert>
      ) : null}
    </Stack>
  );
};

export default ProviderSandboxSetting;
