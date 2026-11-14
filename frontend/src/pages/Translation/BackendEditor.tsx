/* eslint-disable camelcase -- transport names. */
import { FunctionComponent, useMemo, useState } from "react";
import { Alert, Button, Group, Stack, Text } from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
import {
  readRefusal,
  useCreateConnection,
  useDeleteConnection,
  useFieldOptions,
  useUpdateConnection,
} from "@/apis/hooks/translation";
import {
  applyChoice,
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
import ConflictNotice from "./ConflictNotice";
import ConsentModal from "./ConsentModal";
import { backendLabel, StartingPoint } from "./model";
import { useDraftRevision } from "./useDraftRevision";
import { useGuardedSave } from "./useGuardedSave";
import styles from "./Translation.module.scss";

export interface BackendEditorProps {
  engine: TranslationEngine;
  /** The back end to edit, or null to add one. */
  connection: TranslationConnection | null;
  /** Where a new back end starts from: its type, and the preset when it has one. */
  start?: StartingPoint;
  onClose: () => void;
}

/**
 * The form of one back end, drawn from its type's schema. It opens inside the
 * list, under the row it edits, so the order stays in view while editing.
 */
const BackendEditor: FunctionComponent<BackendEditorProps> = ({
  engine,
  connection,
  start,
  onClose,
}) => {
  const types = engine.schema?.backend_types ?? {};
  const backendType = connection?.backend_type ?? start?.typeId ?? "";
  const definition = types[backendType];
  const fields = useMemo(() => definition?.fields ?? [], [definition]);
  const [values, setValues] = useState<PluginValues>(() => {
    const stored = initialValues(fields, connection?.values ?? {});
    return start?.preset
      ? applyChoice(start.preset.field, start.preset.choice, stored)
      : stored;
  });
  const [cleared, setCleared] = useState<string[]>([]);
  const [removing, setRemoving] = useState(false);
  const [removeProblem, setRemoveProblem] = useState("");
  const create = useCreateConnection();
  const update = useUpdateConnection();
  const remove = useDeleteConnection();
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
  const discard = () => {
    if (!connection) {
      return;
    }
    setValues(initialValues(fields, connection.values));
    setCleared([]);
    revision.reload(connection.revision);
    form.dismiss();
  };

  const name = connection
    ? backendLabel(engine, connection)
    : (start?.label ?? definition?.label ?? "back end");

  return (
    <div className={styles.editor}>
      <Stack className={styles.editorForm}>
        {definition?.description && (
          <Text size="sm" c="dimmed">
            {definition.description}
          </Text>
        )}
        {connection?.awaiting_secrets && (
          <Alert color="yellow" title="Enter the key again">
            This back end&apos;s address changed, so its saved key no longer
            applies and was not sent to the new address.
          </Alert>
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
        {form.stale ? (
          <ConflictNotice
            subject="This back end"
            busy={form.busy}
            onSaveMine={() => void save()}
            onDiscard={discard}
          />
        ) : (
          form.message && (
            <Alert color="red" role="alert">
              {form.message}
            </Alert>
          )
        )}
        {removeProblem && (
          <Alert color="red" role="alert">
            {removeProblem}
          </Alert>
        )}
      </Stack>
      <div className={styles.editorFooter}>
        {connection ? (
          removing ? (
            <Group gap="xs">
              <Text size="sm">Remove {name} and delete its key?</Text>
              <Button
                size="xs"
                color="red"
                loading={remove.isPending}
                onClick={() =>
                  remove.mutate(
                    { id: connection.id, revision: connection.revision },
                    {
                      onSuccess: onClose,
                      onError: (error) => {
                        setRemoving(false);
                        setRemoveProblem(
                          readRefusal(error)?.message ??
                            "The back end could not be removed. Try again.",
                        );
                      },
                    },
                  )
                }
              >
                Remove
              </Button>
              <Button
                size="xs"
                variant="default"
                onClick={() => setRemoving(false)}
              >
                Keep
              </Button>
            </Group>
          ) : (
            <Button
              variant="subtle"
              color="red"
              size="sm"
              onClick={() => {
                setRemoveProblem("");
                setRemoving(true);
              }}
            >
              Remove back end
            </Button>
          )
        ) : (
          <span />
        )}
        {!form.stale && (
          <Group gap="xs">
            <Button variant="default" onClick={onClose} disabled={form.busy}>
              Cancel
            </Button>
            <Button loading={form.busy} onClick={() => void save()}>
              {connection ? "Save back end" : "Add back end"}
            </Button>
          </Group>
        )}
      </div>
      <ConsentModal
        destinations={form.asking}
        busy={form.busy}
        onAccept={() => void accept()}
        onCancel={form.decline}
      />
    </div>
  );
};

export default BackendEditor;
