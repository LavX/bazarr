import { FunctionComponent } from "react";
import {
  Alert,
  Button,
  Group,
  Stack,
  Text as MantineText,
} from "@mantine/core";
import {
  useFlareSolverrDetect,
  useFlareSolverrStatus,
} from "@/apis/hooks/providers";
import { Text } from "@/pages/Settings/components";
import { useFormActions } from "@/pages/Settings/utilities/FormValues";
import { useSettingValue } from "@/pages/Settings/utilities/hooks";

export const flaresolverrSettingKey = "settings-endpoints-flaresolverr_v1_url";

export const flaresolverrHelp =
  "Used by every provider that supports FlareSolverr and has no URL of its own. There is no per-provider opt-out; give a provider its own URL to override it.";

const FlareSolverrIntegration: FunctionComponent = () => {
  const { setValue } = useFormActions();
  const value = useSettingValue<string>(flaresolverrSettingKey);
  const status = useFlareSolverrStatus();
  const detect = useFlareSolverrDetect();

  // The latest answer wins: pressing Detect replaces what the page read on load.
  const current = detect.data ?? status.data;
  const detected = current?.detected ?? null;
  const typed = (value ?? "").trim();
  const fromEnvironment = current?.source === "env" && typed === "";
  const lookedAndFoundNothing =
    detect.isSuccess && detect.data.detected === null;

  return (
    <Stack gap="sm" maw={720}>
      <MantineText size="sm">
        One FlareSolverr address for every provider that can solve its
        challenges through it.
      </MantineText>
      <Text
        label="FlareSolverr address"
        placeholder="http://flaresolverr:8191"
        description={flaresolverrHelp}
        settingKey={flaresolverrSettingKey}
      />
      {fromEnvironment && (
        <MantineText size="sm">
          Currently taken from the FLARESOLVERR_URL environment variable (
          {current?.url}). An address saved here replaces it.
        </MantineText>
      )}
      {detected && detected !== typed && (
        <Alert color="blue" title="FlareSolverr found">
          <Stack gap="xs">
            <MantineText size="sm">
              A FlareSolverr container answered at {detected}. It is not used
              until you accept it and save.
            </MantineText>
            <Group>
              <Button
                type="button"
                variant="light"
                mih={44}
                onClick={() => setValue(detected, flaresolverrSettingKey)}
              >
                Use {detected}
              </Button>
            </Group>
          </Stack>
        </Alert>
      )}
      <Group>
        <Button
          type="button"
          variant="default"
          mih={44}
          loading={detect.isPending}
          onClick={() => detect.mutate()}
        >
          Detect
        </Button>
        {lookedAndFoundNothing && (
          <MantineText size="sm" role="status">
            Nothing answered at the usual sibling address.
          </MantineText>
        )}
        {detect.isError && (
          <MantineText size="sm" c="red" role="status">
            The check could not be completed.
          </MantineText>
        )}
      </Group>
    </Stack>
  );
};

export default FlareSolverrIntegration;
