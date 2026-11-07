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

/** The engine's own settings, drawn from its manifest. Connections have their own form. */
const EngineSettings: FunctionComponent<{ engine: TranslationEngine }> = ({
  engine,
}) => {
  const fields = engine.schema?.fields ?? [];
  const [values, setValues] = useState<PluginValues>(() =>
    initialValues(fields, engine.values),
  );
  const [cleared, setCleared] = useState<string[]>([]);
  const mutation = useSaveEngine();
  const form = useGuardedSave((accepted) =>
    mutation.mutateAsync({
      id: engine.id,
      body: {
        expected_revision: engine.revision,
        values,
        clear_secrets: cleared,
        accept_destinations: accepted,
      },
    }),
  );

  return (
    <Stack>
      <PluginSettingsForm
        fields={fields}
        values={values}
        secrets={engine.secrets}
        errors={form.fieldErrors}
        disabled={form.busy}
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
