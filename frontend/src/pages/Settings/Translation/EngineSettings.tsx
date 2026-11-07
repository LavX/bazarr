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
import ConsentModal from "./ConsentModal";
import { useGuardedSave } from "./useGuardedSave";

/**
 * The engine's own settings, drawn from its manifest. Connections have their
 * own form. The form keeps what was typed when a save is refused, and takes the
 * current revision from the engine it is given, so after a conflict the user
 * sees the refusal and a second save goes through.
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
  const form = useGuardedSave(async (accepted) => {
    const saved = await mutation.mutateAsync({
      id: engine.id,
      body: {
        expected_revision: engine.revision,
        values,
        clear_secrets: cleared,
        accept_destinations: accepted,
      },
    });
    // Typed secrets leave the form once they are saved; the rest follows the server.
    setValues(initialValues(fields, saved.engine.values));
    setCleared([]);
    return saved;
  });

  return (
    <Stack>
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
        <Alert color="yellow" title="Secrets needed">
          An address changed, so the saved keys no longer apply. Enter them
          again.
        </Alert>
      )}
      {form.message && (
        <Alert color="red" role="alert">
          {form.message}
          {form.stale && " Your changes are kept. Save again to overwrite."}
        </Alert>
      )}
      <Group>
        <Button loading={form.busy} onClick={() => void form.save()}>
          Save engine settings
        </Button>
      </Group>
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
