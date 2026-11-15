/* eslint-disable camelcase -- transport names. */
import { FunctionComponent, useState } from "react";
import { Alert, Button, Group, Stack } from "@mantine/core";
import { useFreshState, useSaveEngine } from "@/apis/hooks/translation";
import {
  initialValues,
  PluginSettingsForm,
  PluginValues,
} from "@/components/plugin-settings";
import type { TranslationEngine } from "@/types/translation";
import ConflictNotice from "./ConflictNotice";
import ConsentModal from "./ConsentModal";
import { useDraftRevision } from "./useDraftRevision";
import { useGuardedSave } from "./useGuardedSave";
import styles from "./Translation.module.scss";

/**
 * The engine's own settings, drawn from its manifest. Back ends have their
 * own form. The draft keeps the revision it was loaded from, so a save made
 * after another tab's save is refused. The form keeps what was typed when that
 * happens, and saving over the other change is then an explicit choice.
 */
const EngineSettings: FunctionComponent<{ engine: TranslationEngine }> = ({
  engine,
}) => {
  const fields = engine.schema?.fields ?? [];
  const [values, setValues] = useState<PluginValues>(() =>
    initialValues(fields, engine.values),
  );
  const [cleared, setCleared] = useState<string[]>([]);
  const mutation = useSaveEngine();
  const revision = useDraftRevision(engine.revision);
  const form = useGuardedSave(async (accepted) => {
    const saved = await mutation.mutateAsync({
      id: engine.id,
      body: {
        expected_revision: revision.forSave(),
        values,
        clear_secrets: cleared,
        accept_destinations: accepted,
      },
    });
    // Typed secrets leave the form once they are saved; the rest follows the server.
    setValues(initialValues(fields, saved.engine.values));
    setCleared([]);
    revision.reload(saved.engine.revision);
    return saved;
  }, revision.conflicted);

  const fresh = useFreshState();
  // Which answer to the conflict is waiting for what is saved now.
  const [loadingFor, setLoadingFor] = useState<"mine" | "discard" | null>(null);
  const [reloadProblem, setReloadProblem] = useState("");
  // What is saved now, loaded for this answer. The cached state is not used: the refresh a conflict starts
  // may still be on its way, and copying the old state would bring back the very revision that was refused.
  const savedNow = async (action: "mine" | "discard") => {
    setLoadingFor(action);
    setReloadProblem("");
    try {
      const state = await fresh();
      return state.instances.find((item) => item.id === engine.id) ?? null;
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
    setValues(initialValues(saved.schema?.fields ?? fields, saved.values));
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
    await form.save();
  };

  return (
    <Stack className={styles.editorForm}>
      <PluginSettingsForm
        fields={fields}
        values={values}
        secrets={engine.secrets}
        errors={form.fieldErrors}
        // Locked while a conflict answer loads what is saved now: the save that follows sends what the
        // form held when the answer was given, so nothing may be typed in between.
        disabled={form.busy || loadingFor !== null}
        pendingClear={cleared}
        onChange={setValues}
        onClearSecret={(key) =>
          setCleared((current) => [...new Set([...current, key])])
        }
      />
      {engine.awaiting_secrets && (
        <Alert color="yellow" title="Enter the keys again">
          An address changed, so the saved keys no longer apply.
        </Alert>
      )}
      {form.stale ? (
        <ConflictNotice
          subject="These settings"
          saving={form.busy || loadingFor === "mine"}
          reloading={loadingFor === "discard"}
          problem={reloadProblem}
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
      {!form.stale && (
        <Group>
          <Button loading={form.busy} onClick={() => void form.save()}>
            Save engine settings
          </Button>
        </Group>
      )}
      <ConsentModal
        destinations={form.asking}
        busy={form.busy}
        onAccept={() => void form.accept()}
        onCancel={form.decline}
      />
    </Stack>
  );
};

export default EngineSettings;
