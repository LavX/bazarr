/* eslint-disable camelcase */

import { FC, useCallback, useMemo, useState } from "react";
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
} from "@mantine/core";
import {
  useMediaServerLibraries,
  useMediaServerTest,
} from "@/apis/hooks/mediaServers";
import type { MediaServerOptions, PathMapping } from "@/apis/raw/mediaServers";
import { KINDS_WITH_PATH_MAPPINGS } from "@/apis/raw/mediaServers";
import {
  CREDENTIAL_LABELS,
  kindName,
  URL_PLACEHOLDERS,
} from "@/pages/Settings/MediaServers/kinds";
import LibraryPickers from "@/pages/Settings/MediaServers/LibraryPickers";
import PathMappings from "@/pages/Settings/MediaServers/PathMappings";
import StepLayout from "@/pages/Setup/StepLayout";
import type { WizardStepProps } from "@/pages/Setup/steps/types";
import type { MediaServerDraft } from "@/pages/Setup/useOnboardingSelection";
import { useOnboardingSelection } from "@/pages/Setup/useOnboardingSelection";
import StepActions from "./StepActions";
import {
  DraftFieldErrors,
  isDraftTouched,
  useMediaServerSubmit,
} from "./submit";
import styles from "./InstanceServerForm.module.scss";

interface Props extends WizardStepProps {
  // Plex is not here: its connection comes from the account flow, not a form.
  draft: MediaServerDraft;
}

/**
 * Onboarding form for one media server whose connection is typed in: Emby,
 * Jellyfin or Silo. It writes through the same create the Connections instance
 * form uses and reuses the same PathMappings and LibraryPickers editors, so a
 * row made here is one Settings can re-save.
 *
 * The kind decides the fields. Emby and Silo resolve a publication by path, so
 * they need mappings or the dispatcher never addresses them; Jellyfin resolves
 * the item itself and needs the libraries it should scan instead. Plex does
 * neither and has its own panel.
 *
 * One form per server, always: the test and libraries hooks close over the
 * connection values at hook-call time and reset their result when one of them
 * changes, which is what keeps a stale "Connected" from sitting under an edited
 * URL. That behaviour only survives if each server has a component of its own.
 *
 * Continue with nothing filled in writes nothing and advances, the same
 * contract as every other optional connection step.
 */
const InstanceServerForm: FC<Props> = ({ draft, onNext, onBack }) => {
  const kind = draft.kind;
  const name = kindName(kind);
  const credential = CREDENTIAL_LABELS[kind];
  const mapped = KINDS_WITH_PATH_MAPPINGS.includes(kind);
  const { updateDraft, markSaved } = useOnboardingSelection();
  // Emby and Silo cannot be saved without a mapping, so the first row is on
  // screen from the start. It used to sit behind an Add mapping button with no
  // required marker, which the reader only found after the save was refused.
  const mappings =
    mapped && draft.pathMappings.length === 0
      ? [{ local_path: "", remote_path: "" }]
      : draft.pathMappings;
  const { submit, isPending } = useMediaServerSubmit();

  const [errors, setErrors] = useState<DraftFieldErrors>({});
  const [failure, setFailure] = useState<string | null>(null);
  // Held on the draft, not in component state: pressing Back and walking
  // forward again remounts this form, and a warning that disappeared on the
  // way would be as good as never shown.
  const savedButNotSwitchedOn = draft.savedInstanceId !== undefined;

  // Typing is the reader answering the message, so the message goes as they
  // answer it. It used to sit there until Test was pressed.
  const clear = useCallback((field: keyof DraftFieldErrors) => {
    setFailure(null);
    setErrors((current) =>
      current[field] === undefined
        ? current
        : { ...current, [field]: undefined },
    );
  }, []);

  const set = useCallback(
    (patch: Partial<MediaServerDraft>, field?: keyof DraftFieldErrors) => {
      if (field) clear(field);
      updateDraft(draft.draftId, patch);
    },
    [clear, draft.draftId, updateDraft],
  );

  const connection = useMemo(
    () => ({
      url: draft.url.trim(),
      api_key: draft.apiKey.trim(),
      verify_ssl: draft.verifySsl,
    }),
    [draft.url, draft.apiKey, draft.verifySsl],
  );
  const configured = connection.url.length > 0 && connection.api_key.length > 0;
  const touched = isDraftTouched(draft);

  const test = useMediaServerTest(kind, connection);
  const libraries = useMediaServerLibraries(kind, connection);

  const handleContinue = () => {
    if (!touched) {
      onNext();
      return;
    }
    // The server was written on an earlier press and only its master switch
    // failed, so this press is the reader accepting that and moving on. It must
    // not write the same server a second time.
    if (draft.savedInstanceId !== undefined) {
      markSaved(draft.draftId, draft.savedInstanceId);
      onNext();
      return;
    }
    setFailure(null);
    void submit([draft]).then((result) => {
      const found = result.errors[draft.draftId];
      if (found) {
        setErrors(found);
        return;
      }
      setErrors({});
      const outcome = result.outcomes[0];
      if (!outcome?.ok) {
        setFailure(outcome?.error ?? "The save failed.");
        return;
      }
      if (result.switchesFailed) {
        // Marking the draft saved is what removes this step from the wizard,
        // so doing it here would unmount the warning in the same commit that
        // renders it: the reader would reach Finish with the server reported
        // as connected and nothing refreshing. The step stays until they have
        // read it and pressed Continue again.
        updateDraft(draft.draftId, {
          savedInstanceId: outcome.instanceId ?? "",
        });
        return;
      }
      markSaved(draft.draftId, outcome.instanceId ?? "");
      onNext();
    });
  };

  // Everything the reader reads rather than types into: the connection verdict
  // and anything that went wrong. It sits with the explanation, which is what
  // keeps the fields beside it instead of under it.
  const status = (
    <Stack gap="sm">
      <Group gap="sm" align="center">
        <Button
          type="button"
          variant="light"
          disabled={!configured}
          loading={test.isPending}
          onClick={() => {
            setFailure(null);
            test.mutate();
          }}
        >
          Test
        </Button>
        {test.isSuccess && test.data.success ? (
          <Text size="sm" c="green">
            {test.data.server_name
              ? `Connected to ${test.data.server_name}`
              : "Connection succeeded"}
            {kind !== "silo" && test.data.version
              ? ` (v${test.data.version})`
              : ""}
            . Test checks access, not refresh permission.
          </Text>
        ) : test.isError || (test.isSuccess && !test.data.success) ? (
          <Text size="sm" c="red">
            Connection test failed. Check the Server URL, {credential},
            certificate and server access.
          </Text>
        ) : (
          <Text size="sm" c="dimmed">
            {configured
              ? "Test checks access, not refresh permission."
              : `Enter a Server URL and ${credential} to test this connection.`}
          </Text>
        )}
      </Group>

      {failure && (
        <Alert
          color="red"
          title={`Could not save ${draft.name.trim() || name}`}
        >
          {failure} Try again, or connect it later from Settings, Connections.
        </Alert>
      )}
      {savedButNotSwitchedOn && (
        <Alert color="red" title={`${name} is saved but switched off`}>
          The instance was saved, but its {name} master switch could not be
          turned on, so nothing refreshes yet. Enable it in Settings,
          Connections, then continue.
        </Alert>
      )}

      {/* Loading the libraries is a read, and what it says afterwards is a
          verdict, so both sit here rather than above the editor they feed. It
          also keeps the editor at the top of the controls column instead of
          under a paragraph that grows to three lines when nothing came back. */}
      {kind === "jellyfin" && libraries.isIdle && (
        <Stack gap={6} align="flex-start">
          <Button
            type="button"
            variant="light"
            disabled={!configured}
            onClick={() => libraries.mutate()}
          >
            Load libraries
          </Button>
          <Text size="sm" c="dimmed">
            {configured
              ? "Optional. Leave it and this instance refreshes whatever the item resolves to."
              : `Enter a Server URL and ${credential} first.`}
          </Text>
        </Stack>
      )}
      {kind === "silo" && (
        <Stack gap={6} align="flex-start">
          <Button
            type="button"
            variant="light"
            disabled={!configured}
            loading={libraries.isPending}
            onClick={() => libraries.mutate()}
          >
            Load libraries
          </Button>
          {libraries.isError ? (
            <Text size="sm" c="red">
              Could not load Silo libraries. Check the Server URL, {credential}{" "}
              and library access.
            </Text>
          ) : libraries.isSuccess && libraries.data.length === 0 ? (
            <Text size="sm" c="dimmed">
              No supported libraries found. Enable a movie or series library in
              Silo and check the API key&apos;s access.
            </Text>
          ) : (
            <Text size="sm" c="dimmed">
              Every Silo mapping is scoped to a library, so the mappings open
              once the libraries are loaded.
            </Text>
          )}
        </Stack>
      )}
    </Stack>
  );

  return (
    <StepLayout
      title={draft.name.trim() || name}
      titleOrder={3}
      description={`Connect ${name} so Bazarr can refresh it after it downloads subtitles.`}
      aside={status}
      actions={
        <StepActions
          onNext={onNext}
          onBack={onBack}
          onContinue={handleContinue}
          continueLabel={
            savedButNotSwitchedOn
              ? "Continue anyway"
              : !touched
                ? `Continue without ${name}`
                : failure
                  ? `Try ${name} again`
                  : `Connect ${name}`
          }
          continuePending={isPending}
        />
      }
    >
      <div className={styles.fields}>
        <TextInput
          label="Name"
          description="Shown in Settings, Connections"
          value={draft.name}
          error={errors.name}
          onChange={(event) => set({ name: event.currentTarget.value }, "name")}
        />
        <TextInput
          label="Server URL"
          placeholder={URL_PLACEHOLDERS[kind]}
          description={
            kind === "silo"
              ? `Full URL of your ${name} server, including any path prefix. Keep credentials in the ${credential} field. Refreshes need an IP address or a name resolved by DNS or the hosts file: mDNS and other local-discovery names pass the Test but fail refreshes.`
              : `Full URL of your ${name} server, including any path prefix. Keep credentials in the ${credential} field.`
          }
          value={draft.url}
          error={errors.url}
          onChange={(event) => set({ url: event.currentTarget.value }, "url")}
        />
        <PasswordInput
          label={credential}
          autoComplete="new-password"
          value={draft.apiKey}
          error={errors.apiKey}
          onChange={(event) =>
            set({ apiKey: event.currentTarget.value }, "apiKey")
          }
        />
        <Switch
          label="Verify SSL certificate"
          description="Applies to HTTPS connections."
          checked={draft.verifySsl}
          onChange={(event) => set({ verifySsl: event.currentTarget.checked })}
        />
      </div>

      {kind === "jellyfin" && !libraries.isIdle && (
        <>
          <Divider label="Libraries this instance refreshes" />
          <LibraryPickers
            kind="jellyfin"
            value={draft.options}
            onChange={(options: MediaServerOptions) => set({ options })}
            libraries={libraries}
            configured={configured}
          />
        </>
      )}

      {mapped && (
        <>
          <Divider label="Path mappings (required)" />
          {/* Silo cannot add a mapping before its libraries are known: the
              editor's own Add control is disabled until then. */}
          {(kind !== "silo" || libraries.isSuccess) && (
            <PathMappings
              kind={kind}
              value={mappings}
              onChange={(pathMappings: PathMapping[]) =>
                set({ pathMappings }, "pathMappings")
              }
              libraries={libraries.data}
            />
          )}
          {errors.pathMappings && (
            <Text size="sm" c="red">
              {errors.pathMappings}
            </Text>
          )}
        </>
      )}
    </StepLayout>
  );
};

export default InstanceServerForm;
