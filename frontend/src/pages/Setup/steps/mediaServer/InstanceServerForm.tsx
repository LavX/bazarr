/* eslint-disable camelcase */

import { FC, useState } from "react";
import {
  Alert,
  Button,
  Divider,
  Group,
  PasswordInput,
  Stack,
  Switch,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { useSettingsMutation } from "@/apis/hooks";
import {
  useMediaServerLibraries,
  useMediaServerTest,
  useSaveMediaServerInstance,
} from "@/apis/hooks/mediaServers";
import type {
  MediaServerKind,
  MediaServerOptions,
  PathMapping,
} from "@/apis/raw/mediaServers";
import { KINDS_WITH_PATH_MAPPINGS } from "@/apis/raw/mediaServers";
import {
  CREDENTIAL_LABELS,
  kindName,
  URL_PLACEHOLDERS,
} from "@/pages/Settings/MediaServers/kinds";
import LibraryPickers from "@/pages/Settings/MediaServers/LibraryPickers";
import PathMappings from "@/pages/Settings/MediaServers/PathMappings";
import type { WizardStepProps } from "@/pages/Setup/steps/types";
import StepActions from "./StepActions";
import { validateServerUrl } from "./validation";

interface Props extends WizardStepProps {
  // Plex is not here: its connection comes from the account flow, not a form.
  kind: Exclude<MediaServerKind, "plex">;
}

/**
 * Onboarding form for the kinds whose connection is typed in: Emby, Jellyfin
 * and Silo. It saves through useSaveMediaServerInstance, the writer the
 * Connections instance form uses, and reuses the same PathMappings and
 * LibraryPickers editors, so a row made here is one Settings can re-save.
 *
 * The kind decides the fields. Emby and Silo resolve a publication by path, so
 * they need mappings or the dispatcher never addresses them; Jellyfin resolves
 * the item itself and needs the libraries it should scan instead. Plex does
 * neither and is not offered here.
 *
 * Continue with nothing filled in writes nothing and advances, the same
 * contract as every other optional connection step.
 */
const InstanceServerForm: FC<Props> = ({ kind, onNext, onBack }) => {
  const name = kindName(kind);
  const credential = CREDENTIAL_LABELS[kind];
  const mapped = KINDS_WITH_PATH_MAPPINGS.includes(kind);
  const settings = useSettingsMutation();

  const [displayName, setDisplayName] = useState(name);
  const [url, setUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [verifySsl, setVerifySsl] = useState(true);
  const [mappings, setMappings] = useState<PathMapping[]>([]);
  const [options, setOptions] = useState<MediaServerOptions>({});
  const [problem, setProblem] = useState<string | null>(null);

  const connection = {
    url: url.trim(),
    api_key: apiKey.trim(),
    verify_ssl: verifySsl,
  };
  const configured = connection.url.length > 0 && connection.api_key.length > 0;
  const touched =
    url.trim().length > 0 || apiKey.trim().length > 0 || mappings.length > 0;
  const test = useMediaServerTest(kind, connection);
  const libraries = useMediaServerLibraries(kind, connection);
  const save = useSaveMediaServerInstance(kind, {
    ...connection,
    name: displayName.trim(),
    enabled: true,
    path_mappings: mappings,
    options,
  });

  const incompleteMapping = mappings.some(
    (mapping) => !mapping.local_path.trim() || !mapping.remote_path.trim(),
  );

  const validate = (): string | null => {
    if (!displayName.trim()) return "Name is required";
    const urlProblem = validateServerUrl(connection.url);
    if (urlProblem) return urlProblem;
    if (!connection.api_key) return `${credential} is required`;
    if (mapped && mappings.length === 0)
      return "An enabled instance needs at least one path mapping";
    if (incompleteMapping)
      return "Fill in both paths on every mapping, or remove it";
    return null;
  };

  const handleContinue = () => {
    if (!touched) {
      onNext();
      return;
    }
    const found = validate();
    setProblem(found);
    if (found) return;
    save.mutate(undefined, {
      onSuccess: () => {
        // The row is what refreshes; the master switch is what lets the
        // dispatcher publish it, exactly as the Connections page stages both.
        settings.mutate(
          { [`settings-general-use_${kind}`]: true },
          { onSuccess: () => onNext() },
        );
      },
    });
  };

  return (
    <Stack gap="lg">
      <Stack gap="xs">
        <Title order={3}>{name}</Title>
        <Text c="dimmed">
          Connect {name} so Bazarr can refresh it after it downloads subtitles.
        </Text>
      </Stack>

      <TextInput
        label="Name"
        description="Shown in Settings, Connections"
        value={displayName}
        onChange={(event) => setDisplayName(event.currentTarget.value)}
      />
      <TextInput
        label="Server URL"
        placeholder={URL_PLACEHOLDERS[kind]}
        description={`Full URL of your ${name} server, including any path prefix. Keep credentials in the ${credential} field.`}
        value={url}
        onChange={(event) => setUrl(event.currentTarget.value)}
      />
      {kind === "silo" && (
        <Text size="sm" c="dimmed">
          Refreshes need an IP address or a name resolved by DNS or the hosts
          file. mDNS and other local-discovery names pass the Test but fail
          refreshes.
        </Text>
      )}
      <PasswordInput
        label={credential}
        autoComplete="new-password"
        value={apiKey}
        onChange={(event) => setApiKey(event.currentTarget.value)}
      />
      <Switch
        label="Verify SSL certificate"
        checked={verifySsl}
        onChange={(event) => setVerifySsl(event.currentTarget.checked)}
      />
      <Text size="xs" c="dimmed">
        Certificate verification applies to HTTPS connections.
      </Text>

      <Group>
        <Button
          type="button"
          variant="light"
          disabled={!configured}
          loading={test.isPending}
          onClick={() => {
            setProblem(null);
            test.mutate();
          }}
        >
          Test
        </Button>
      </Group>
      <Text size="sm" c="dimmed">
        Test checks access, not refresh permission.
      </Text>
      {!configured && (
        <Text size="sm" c="dimmed">
          Enter a Server URL and {credential} to test this connection.
        </Text>
      )}
      {test.isSuccess && test.data.success && (
        <Alert color="green">
          {test.data.server_name
            ? `Connected to ${test.data.server_name}`
            : "Connection succeeded"}
          {kind !== "silo" && test.data.version
            ? ` (v${test.data.version})`
            : ""}
          .
        </Alert>
      )}
      {(test.isError || (test.isSuccess && !test.data.success)) && (
        <Alert color="red">
          Connection test failed. Check the Server URL, {credential},
          certificate and server access.
        </Alert>
      )}

      {kind === "jellyfin" && (
        <>
          <Divider label="Libraries this instance refreshes" />
          <LibraryPickers
            kind="jellyfin"
            value={options}
            onChange={setOptions}
            libraries={libraries}
            configured={configured}
          />
        </>
      )}

      {mapped && (
        <>
          <Divider label="Path mappings" />
          {kind === "silo" && (
            <>
              <Group>
                <Button
                  type="button"
                  variant="light"
                  disabled={!configured}
                  loading={libraries.isPending}
                  onClick={() => libraries.mutate()}
                >
                  Load libraries
                </Button>
              </Group>
              {libraries.isPending && (
                <Text size="sm">Loading Silo libraries...</Text>
              )}
              {libraries.isError && (
                <Alert color="red">
                  Could not load Silo libraries. Check the Server URL,{" "}
                  {credential} and library access.
                </Alert>
              )}
              {libraries.isSuccess && libraries.data.length === 0 && (
                <Alert color="gray">
                  No supported libraries found. Enable a movie or series library
                  in Silo and check the API key's access.
                </Alert>
              )}
              {libraries.isIdle && (
                <Text size="sm" c="dimmed">
                  Load libraries to add mappings or choose an available Silo
                  library.
                </Text>
              )}
            </>
          )}
          <PathMappings
            kind={kind}
            value={mappings}
            onChange={setMappings}
            libraries={libraries.data}
          />
        </>
      )}

      {problem && (
        <Alert color="red" title={`Could not connect ${name}`}>
          {problem}
        </Alert>
      )}
      {save.isError && (
        <Alert color="red">
          Could not save this instance. Check{" "}
          {mapped
            ? `the name, the full URL, the ${credential.toLowerCase()} and the path mappings`
            : `the name, the full URL and the ${credential.toLowerCase()}`}
          , then try again, or connect it later from Settings, Connections.
        </Alert>
      )}
      {settings.isError && (
        <Alert color="red">
          The instance was saved, but its {name} master switch could not be
          turned on, so nothing refreshes yet. Enable it in Settings,
          Connections.
        </Alert>
      )}

      <StepActions
        onNext={onNext}
        onBack={onBack}
        onContinue={handleContinue}
        continueLabel={touched ? `Connect ${name}` : `Continue without ${name}`}
        continuePending={save.isPending || settings.isPending}
      />
    </Stack>
  );
};

export default InstanceServerForm;
