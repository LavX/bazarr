/* eslint-disable camelcase */

import { FormEvent, useState } from "react";
import {
  Alert,
  Button,
  Divider,
  Group,
  Modal,
  PasswordInput,
  SegmentedControl,
  Stack,
  Switch,
  Text,
  TextInput,
} from "@mantine/core";
import { useForm } from "@mantine/form";
import {
  useMediaServerLibraries,
  useMediaServerTest,
  useSaveMediaServerInstance,
} from "@/apis/hooks/mediaServers";
import type {
  ConnectionOverrides,
  MediaServerInstance,
  MediaServerKind,
  MediaServerOptions,
  PathMapping,
} from "@/apis/raw/mediaServers";
import {
  KINDS_WITH_LIBRARIES,
  KINDS_WITH_PATH_MAPPINGS,
} from "@/apis/raw/mediaServers";
import { CREDENTIAL_LABELS, kindName, URL_PLACEHOLDERS } from "./kinds";
import LibraryPickers from "./LibraryPickers";
import PathMappings from "./PathMappings";

type KeyMode = "keep" | "replace" | "clear";
interface Props {
  kind: MediaServerKind;
  instance: MediaServerInstance | null;
  onClose: () => void;
}

// Mount one editor per opening so its state and mutation observers cannot be
// reused by a different instance or a later opening of the same instance.
export default function MediaServerInstanceFormModal({
  kind,
  instance,
  onClose,
}: Props) {
  const name = kindName(kind);
  const mapped = KINDS_WITH_PATH_MAPPINGS.includes(kind);
  const scoped = KINDS_WITH_LIBRARIES.includes(kind);
  const credential = CREDENTIAL_LABELS[kind];
  const [keyMode, setKeyMode] = useState<KeyMode>(
    instance ? "keep" : "replace",
  );
  const form = useForm({
    initialValues: {
      name: instance?.name ?? "",
      enabled: instance?.enabled ?? false,
      url: instance?.url ?? "",
      apiKey: "",
      verifySsl: instance?.verify_ssl ?? true,
      mappings: instance?.path_mappings ?? ([] as PathMapping[]),
      refreshMovies: instance?.refresh_movies ?? true,
      refreshEpisodes: instance?.refresh_episodes ?? true,
      options: (instance?.options ?? {}) as MediaServerOptions,
    },
    validate: {
      name: (value) => (value.trim() ? null : "Name is required"),
      url: (value) => {
        try {
          const parsed = new URL(value);
          return ["http:", "https:"].includes(parsed.protocol) &&
            !parsed.username &&
            !parsed.password &&
            !parsed.search &&
            !parsed.hash
            ? null
            : "Enter a full HTTP or HTTPS URL without credentials, query or fragment";
        } catch {
          return "Enter a full HTTP or HTTPS URL";
        }
      },
      apiKey: (value, values) =>
        values.enabled &&
        !(keyMode === "keep"
          ? instance?.api_key_set
          : keyMode === "replace" && value.trim())
          ? `An enabled instance needs ${credential === "Plex token" ? "a Plex token" : "an API key"}`
          : null,
      // Only a kind that resolves by path needs one. Jellyfin and Plex resolve
      // the item themselves and have never had mappings to configure.
      mappings: (value, values) =>
        mapped && values.enabled && value.length === 0
          ? "An enabled instance needs at least one path mapping"
          : null,
    },
  });
  const credentials: ConnectionOverrides =
    keyMode === "clear"
      ? { clear_api_key: true }
      : keyMode === "replace"
        ? { api_key: form.values.apiKey }
        : {};
  const connection = {
    url: form.values.url,
    verify_ssl: form.values.verifySsl,
    ...credentials,
  };
  const configured = Boolean(
    form.values.url.trim() &&
    (keyMode === "keep"
      ? instance?.api_key_set
      : keyMode === "replace" && form.values.apiKey.trim()),
  );
  const test = useMediaServerTest(kind, connection, instance?.id);
  const libraries = useMediaServerLibraries(kind, connection, instance?.id);
  const save = useSaveMediaServerInstance(
    kind,
    {
      ...connection,
      name: form.values.name,
      enabled: form.values.enabled,
      path_mappings: form.values.mappings,
      refresh_movies: form.values.refreshMovies,
      refresh_episodes: form.values.refreshEpisodes,
      options: form.values.options,
    },
    instance?.id,
  );
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    event.stopPropagation();
    if (!save.isPending && !form.validate().hasErrors)
      save.mutate(undefined, { onSuccess: onClose });
  };
  return (
    <Modal
      opened
      onClose={onClose}
      title={`${instance ? "Edit" : "Add"} ${name} instance`}
      centered
      size="lg"
    >
      <form
        onSubmit={submit}
        onKeyDown={(event) => {
          if ((event.ctrlKey || event.metaKey) && event.key === "s") {
            event.preventDefault();
            event.stopPropagation();
            event.currentTarget.requestSubmit();
          }
        }}
      >
        <Stack gap="md">
          <TextInput label="Name" {...form.getInputProps("name")} />
          <Switch
            label="Instance enabled"
            {...form.getInputProps("enabled", { type: "checkbox" })}
          />
          <Text size="sm" c="dimmed">
            The saved {name} master switch must also be enabled for refreshes.
          </Text>
          <TextInput
            label="Server URL"
            placeholder={URL_PLACEHOLDERS[kind]}
            description={`Full URL of your ${name} server, including any path prefix. Keep credentials in the ${credential} field.`}
            {...form.getInputProps("url")}
          />
          {kind === "silo" && (
            <Text size="sm" c="dimmed">
              Refreshes need an IP address or a name resolved by DNS or the
              hosts file. mDNS and other local-discovery names pass the Test but
              fail refreshes.
            </Text>
          )}
          {instance && (
            <>
              <Text size="sm">
                {instance.api_key_set
                  ? `A ${credential.toLowerCase()} is stored. Keep it, replace it or clear it.`
                  : `No ${credential.toLowerCase()} is stored.`}
              </Text>
              <SegmentedControl
                aria-label={`${credential} action`}
                value={keyMode}
                onChange={(value) => {
                  setKeyMode(value as KeyMode);
                  form.setFieldValue("apiKey", "");
                }}
                data={[
                  { value: "keep", label: "Keep" },
                  { value: "replace", label: "Replace" },
                  { value: "clear", label: "Clear" },
                ]}
              />
            </>
          )}
          {keyMode === "replace" && (
            <PasswordInput
              label={credential}
              autoComplete="new-password"
              {...form.getInputProps("apiKey")}
            />
          )}
          {keyMode !== "replace" && form.errors.apiKey && (
            <Text size="sm" c="red">
              {form.errors.apiKey}
            </Text>
          )}
          {keyMode === "clear" && (
            <Alert color="yellow">
              The saved {credential.toLowerCase()} will be removed when you
              save. Disable this instance to save without one.
            </Alert>
          )}
          <Switch
            label="Verify SSL certificate"
            {...form.getInputProps("verifySsl", { type: "checkbox" })}
          />
          <Text size="xs" c="dimmed">
            Certificate verification applies to HTTPS connections.
          </Text>
          <Group>
            <Button
              type="button"
              disabled={!configured}
              loading={test.isPending}
              onClick={() => test.mutate()}
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
          <Text size="sm" c="dimmed">
            Test{kind === "emby" ? " uses" : " and Load libraries use"} the
            current fields, including unsaved changes.
          </Text>
          <Divider label="What this instance refreshes" />
          <Switch
            label="Refresh movies"
            {...form.getInputProps("refreshMovies", { type: "checkbox" })}
          />
          <Switch
            label="Refresh episodes"
            {...form.getInputProps("refreshEpisodes", { type: "checkbox" })}
          />
          {scoped && (
            <LibraryPickers
              kind={kind as "jellyfin" | "plex"}
              value={form.values.options}
              onChange={(value) => form.setFieldValue("options", value)}
              libraries={libraries}
              configured={configured}
            />
          )}
          {kind === "jellyfin" && (
            <>
              <Text size="sm">Refresh method</Text>
              <SegmentedControl
                aria-label="Refresh method"
                value={form.values.options.refresh_method ?? "immediate"}
                onChange={(value) =>
                  form.setFieldValue("options", {
                    ...form.values.options,
                    refresh_method: value as "immediate" | "async",
                  })
                }
                data={[
                  { value: "immediate", label: "Immediate" },
                  { value: "async", label: "Async" },
                ]}
              />
              <Text size="xs" c="dimmed">
                Immediate re-reads the item's metadata straight away. Async
                reports the file change and lets Jellyfin batch the rescan.
              </Text>
            </>
          )}
          {mapped && <Divider label="Path mappings" />}
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
                  Could not load Silo libraries. Check the connection and
                  library access. Saved mappings are preserved.
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
          {mapped && (
            <>
              <PathMappings
                kind={kind}
                value={form.values.mappings}
                onChange={(value) => form.setFieldValue("mappings", value)}
                libraries={libraries.data}
              />
              {form.errors.mappings && (
                <Text size="sm" c="red">
                  {form.errors.mappings}
                </Text>
              )}
            </>
          )}
          {save.isError && (
            <Alert color="red">
              Could not save this instance. Check the name, full URL,{" "}
              {credential.toLowerCase()} and library settings, then try again.
            </Alert>
          )}
          <Group justify="flex-end">
            <Button type="button" variant="default" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" loading={save.isPending}>
              Save instance
            </Button>
          </Group>
        </Stack>
      </form>
    </Modal>
  );
}
