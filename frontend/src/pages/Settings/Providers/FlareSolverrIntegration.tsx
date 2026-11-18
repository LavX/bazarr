import { FunctionComponent, useEffect } from "react";
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
  useFlareSolverrTest,
} from "@/apis/hooks/providers";
import { Text } from "@/pages/Settings/components";
import { useFormActions } from "@/pages/Settings/utilities/FormValues";
import { useSettingValue } from "@/pages/Settings/utilities/hooks";

export const flaresolverrSettingKey = "settings-endpoints-flaresolverr_v1_url";

export const flaresolverrHelp =
  "Used by every provider that supports FlareSolverr and has no URL of its own. There is no per-provider opt-out; give a provider its own URL to override it.";

const errorColor = "var(--mantine-color-error)";

// The server says which address it tested; an empty field means the one in use.
function testOutcome(result: System.FlareSolverrTestResult) {
  const tested =
    result.source === "env"
      ? "Tested the address from the FLARESOLVERR_URL environment variable. "
      : result.source === "setting"
        ? "Tested the saved address. "
        : "";
  return result.ok
    ? `${tested}${result.message}`
    : `Test failed. ${tested}${result.message}`;
}

const FlareSolverrIntegration: FunctionComponent = () => {
  const { setValue } = useFormActions();
  const value = useSettingValue<string>(flaresolverrSettingKey);
  const status = useFlareSolverrStatus();
  const detect = useFlareSolverrDetect();
  const test = useFlareSolverrTest();

  // One source: pressing Detect writes its answer into the status query, and a
  // settings save refetches it, so the newer of the two is always what shows.
  const current = status.data;
  const detected = current?.detected ?? null;
  const typed = (value ?? "").trim();
  // A result belongs to the address it was asked about. Once the field
  // changes, it no longer says anything about what is typed.
  // An empty field tests the address in use, so a change of that address (a
  // save, say) outdates the result as much as typing does.
  const effectiveUrl = current?.url;
  const effectiveSource = current?.source;
  const resetTest = test.reset;
  useEffect(() => {
    resetTest();
  }, [typed, effectiveUrl, effectiveSource, resetTest]);
  // An empty field tests the address in use. Only a loaded status that confirms
  // there is none rules the Test out; while it loads, or if it failed, the
  // server decides.
  const nothingToTest = typed === "" && status.isSuccess && !current?.url;
  const fromEnvironment = current?.source === "env" && typed === "";
  const checked =
    detect.isSuccess && current !== undefined && !current.detecting;
  const nothingAnswered = checked && !current.sibling_found;
  const alreadyInUse = checked && current.sibling_found && detected === null;

  return (
    <Stack gap="sm" maw={720}>
      <MantineText size="sm">
        One FlareSolverr address for every provider that can solve its
        challenges through it.
      </MantineText>
      <Text
        label="FlareSolverr address"
        placeholder="http://flaresolverr:8191/v1"
        description={flaresolverrHelp}
        settingKey={flaresolverrSettingKey}
      />
      <Group align="center" gap="sm">
        <Button
          type="button"
          variant="default"
          mih={44}
          disabled={nothingToTest}
          loading={test.isPending}
          onClick={() => test.mutate(typed)}
        >
          Test
        </Button>
        {/* Always in the page, so a screen reader announces what lands in it. */}
        <MantineText
          size="sm"
          role="status"
          c={test.data && !test.data.ok ? errorColor : undefined}
        >
          {nothingToTest &&
            "Type an address, or set FLARESOLVERR_URL, to test."}
          {test.isPending && "Testing the address."}
          {test.data && testOutcome(test.data)}
          {test.isError && "The test could not be completed."}
        </MantineText>
      </Group>
      {fromEnvironment && (
        <MantineText size="sm">
          Currently taken from the FLARESOLVERR_URL environment variable (
          {current?.url}). An address saved here replaces it.
        </MantineText>
      )}
      {status.data?.detecting === true && (
        <MantineText size="sm" role="status">
          Looking for a FlareSolverr container next to Bazarr.
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
        {alreadyInUse && (
          <MantineText size="sm" role="status">
            FlareSolverr answered at the usual sibling address, which is already
            in use. No new address to offer.
          </MantineText>
        )}
        {nothingAnswered && (
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
