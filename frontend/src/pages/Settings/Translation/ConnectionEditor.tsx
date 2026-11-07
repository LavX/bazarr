/* eslint-disable camelcase -- transport names. */
import { FunctionComponent, useMemo, useState } from "react";
import { Alert, Button, Group, Modal, Select, Stack } from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
import {
  useCreateConnection,
  useFieldOptions,
  useUpdateConnection,
} from "@/apis/hooks/translation";
import {
  initialValues,
  isSecretField,
  PluginSettingsForm,
  PluginValues,
  withDefaults,
} from "@/components/plugin-settings";
import type {
  TranslationConnection,
  TranslationEngine,
} from "@/types/translation";
import ConsentModal from "./ConsentModal";
import { useDraftRevision } from "./useDraftRevision";
import { useGuardedSave } from "./useGuardedSave";

export interface ConnectionEditorProps {
  engine: TranslationEngine;
  /** The connection to edit, or null to add one. */
  connection: TranslationConnection | null;
  onClose: () => void;
}

const ConnectionEditor: FunctionComponent<ConnectionEditorProps> = ({
  engine,
  connection,
  onClose,
}) => {
  const types = engine.schema?.backend_types ?? {};
  const typeIds = Object.keys(types);
  const [backendType, setBackendType] = useState(
    connection?.backend_type ?? typeIds[0] ?? "",
  );
  const definition = types[backendType];
  const fields = useMemo(() => definition?.fields ?? [], [definition]);
  const [values, setValues] = useState<PluginValues>(() =>
    initialValues(definition?.fields ?? [], connection?.values ?? {}),
  );
  const [cleared, setCleared] = useState<string[]>([]);
  const create = useCreateConnection();
  const update = useUpdateConnection();
  // The draft above was loaded from this revision; a refreshed prop does not change it.
  const revision = useDraftRevision(connection?.revision ?? 0);

  // The draft the model list is asked for: what is typed, never a secret.
  const draft = useMemo(() => {
    const shown = withDefaults(fields, values);
    return Object.fromEntries(
      fields
        .filter((field) => !isSecretField(field) && field.key in shown)
        .map((field) => [field.key, shown[field.key]]),
    );
  }, [fields, values]);
  // One request per pause in typing, not one per key.
  const [settledDraft] = useDebouncedValue(draft, 400);
  const options = useFieldOptions(
    engine.id,
    backendType,
    "model",
    settledDraft,
    backendType !== "",
  );

  const form = useGuardedSave(
    (accepted) =>
      connection
        ? update.mutateAsync({
            id: connection.id,
            body: {
              expected_revision: revision.forSave(),
              values,
              clear_secrets: cleared,
              accept_destinations: accepted,
            },
          })
        : create.mutateAsync({
            instanceId: engine.id,
            body: {
              backend_type: backendType,
              values,
              accept_destinations: accepted,
            },
          }),
    revision.conflicted,
  );

  const save = async () => {
    if (await form.save()) {
      onClose();
    }
  };
  const accept = async () => {
    if (await form.accept()) {
      onClose();
    }
  };

  return (
    <Modal
      opened
      onClose={onClose}
      size="lg"
      title={connection ? "Edit back end" : "Add a back end"}
    >
      <Stack>
        {!connection && (
          <Select
            label="Back end"
            data={typeIds.map((id) => ({
              value: id,
              label: types[id].label,
            }))}
            value={backendType}
            allowDeselect={false}
            onChange={(next) => {
              if (next !== null) {
                setBackendType(next);
                setValues(initialValues(types[next]?.fields ?? [], {}));
              }
            }}
          />
        )}
        {definition?.description && (
          <Alert variant="light">{definition.description}</Alert>
        )}
        <PluginSettingsForm
          fields={fields}
          values={values}
          secrets={connection?.secrets}
          errors={form.fieldErrors}
          disabled={form.busy}
          pendingClear={cleared}
          optionsFor={(key) =>
            key === "model"
              ? {
                  available: options.data?.available === true,
                  loading: options.isLoading,
                  items: options.data?.items ?? [],
                }
              : undefined
          }
          onChange={setValues}
          onClearSecret={(key) =>
            setCleared((current) => [...new Set([...current, key])])
          }
        />
        {connection?.awaiting_secrets && (
          <Alert color="yellow" title="Secrets needed">
            This back end&apos;s address changed, so its saved key no longer
            applies. Enter the key again.
          </Alert>
        )}
        {form.message && (
          <Alert color="red" role="alert">
            {form.message}
            {form.stale && " Your changes are kept. Save again to overwrite."}
          </Alert>
        )}
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button loading={form.busy} onClick={() => void save()}>
            {connection ? "Save back end" : "Add back end"}
          </Button>
        </Group>
      </Stack>
      <ConsentModal
        destinations={form.asking}
        busy={form.busy}
        onAccept={() => void accept()}
        onCancel={form.decline}
      />
    </Modal>
  );
};

export default ConnectionEditor;
