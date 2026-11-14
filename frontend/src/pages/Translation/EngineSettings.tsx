/* eslint-disable camelcase -- transport names. */
import { FunctionComponent, useState } from "react";
import { Alert, Button, Group, Stack } from "@mantine/core";
import { useSaveEngine } from "@/apis/hooks/translation";
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

  const discard = () => {
    setValues(initialValues(fields, engine.values));
    setCleared([]);
    revision.reload(engine.revision);
    form.dismiss();
  };

  return (
    <Stack className={styles.editorForm}>
      <PluginSettingsForm
        fields={fields}
        values={values}
        secrets={engine.secrets}
        errors={form.fieldErrors}
        disabled={form.busy}
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
          busy={form.busy}
          onSaveMine={() => void form.save()}
          onDiscard={discard}
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
