/* eslint-disable camelcase -- transport names. */
import { FunctionComponent, useEffect, useMemo, useState } from "react";
import { Alert, Button, Group, Stack, Text } from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
import {
  readRefusal,
  useCreateConnection,
  useDeleteConnection,
  useFieldOptions,
  useFreshState,
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
  /**
   * Told while a save, a reload or the removal is on its way, and again when it is over. The list
   * holds its own controls back meanwhile: replacing this form in the middle would leave its answer
   * with nowhere to go.
   */
  onBusy?: (busy: boolean) => void;
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
  onBusy,
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
  const fresh = useFreshState();
  // Which answer to the conflict is waiting for what is saved now.
  const [loadingFor, setLoadingFor] = useState<"mine" | "discard" | null>(null);
  const [reloadProblem, setReloadProblem] = useState("");
  // What is saved now, loaded for this answer. The cached state is not used: the refresh a conflict starts
  // may still be on its way, and copying the old state would bring back the very revision that was refused.
  const savedNow = async (action: "mine" | "discard") => {
    if (!connection) {
      return null;
    }
    setLoadingFor(action);
    setReloadProblem("");
    try {
      const state = await fresh();
      const found = state.instances
        .flatMap((item) => item.connections)
        .find((item) => item.id === connection.id);
      if (!found) {
        // Removed somewhere else: there is nothing left to edit.
        onClose();
      }
      return found ?? null;
    } catch {
      setReloadProblem(
        "What is saved now could not be loaded, so nothing was changed. Try again.",
      );
      return null;
    } finally {
      setLoadingFor(null);
    }
  };
  const discard = async () => {
    const saved = await savedNow("discard");
    if (!saved) {
      return;
    }
    setValues(initialValues(fields, saved.values));
    setCleared([]);
    revision.reload(saved.revision);
    form.dismiss();
  };
  const saveMine = async () => {
    const saved = await savedNow("mine");
    if (!saved) {
      return;
    }
    revision.adopt(saved.revision);
    await save();
  };

  // One thing at a time on this form: a save, a reload for a conflict answer, or the removal. While
  // one is on its way the others cannot be started, so two writes to the same back end never overlap.
  const working = form.busy || loadingFor !== null;
  const busy = working || remove.isPending;
  useEffect(() => {
    onBusy?.(busy);
    // A form that goes away is not busy any more.
    return () => onBusy?.(false);
  }, [busy, onBusy]);

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
          // Locked while a conflict answer loads what is saved now: the save that follows sends what the
          // form held when the answer was given, so nothing may be typed in between.
          disabled={busy}
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
            saving={form.busy || loadingFor === "mine"}
            reloading={loadingFor === "discard"}
            problem={reloadProblem}
            disabled={remove.isPending}
            onSaveMine={() => void saveMine()}
            onDiscard={() => void discard()}
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
                disabled={working}
                onClick={() =>
                  remove.mutate(
                    { id: connection.id, revision: connection.revision },
                    {
                      onSuccess: onClose,
                      onError: (error) => {
                        setRemoving(false);
                        setRemoveProblem(
                          readRefusal(error)?.message ||
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
                disabled={remove.isPending}
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
              disabled={working}
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
            <Button variant="default" onClick={onClose} disabled={busy}>
              Cancel
            </Button>
            <Button
              loading={form.busy}
              disabled={remove.isPending || loadingFor !== null}
              onClick={() => void save()}
            >
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
