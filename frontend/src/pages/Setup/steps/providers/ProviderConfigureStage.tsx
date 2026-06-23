import { FC, useMemo, useState } from "react";
import {
  Anchor,
  Button,
  Checkbox,
  Divider,
  Group,
  PasswordInput,
  ScrollArea,
  Stack,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { useProviderHubProviders, useSettingsMutation } from "@/apis/hooks";
import type {
  ProviderHubInstallation,
  ProviderHubManifest,
} from "@/apis/raw/providerHub";

type FieldType = "text" | "password" | "checkbox";

interface ConfigField {
  key: string;
  label: string;
  description?: string;
  type: FieldType;
  required: boolean;
}

// First-run shows only the fields a provider actually needs to start working:
// required fields plus secret credentials (username/password/api key). Advanced
// toggles (forced-only, FPS, FlareSolverr, delays, AI-translation flags, ...)
// stay hidden here and remain available later in Settings > Providers, so the
// step does not become an overwhelming wall of inputs.
function essentialFields(fields: ConfigField[]): ConfigField[] {
  return fields.filter((field) => field.required || field.type === "password");
}

// Mirrors schemaToInputs() in Settings/Providers/index.tsx: turn a manifest's
// config_schema.properties into the fields we render. Secret fields (or those
// in manifest.secret_fields) become password inputs; booleans become
// checkboxes; everything else is a plain text input.
function fieldsFromManifest(
  manifest: ProviderHubManifest | undefined,
): ConfigField[] {
  const schema = (manifest as LooseObject | undefined)?.config_schema;
  if (!schema || typeof schema !== "object" || Array.isArray(schema)) {
    return [];
  }
  const properties = (schema as LooseObject).properties;
  if (
    !properties ||
    typeof properties !== "object" ||
    Array.isArray(properties)
  ) {
    return [];
  }
  const secretFields = new Set(
    Array.isArray((manifest as LooseObject)?.secret_fields)
      ? ((manifest as LooseObject).secret_fields as string[])
      : [],
  );
  const requiredKeys = new Set(
    Array.isArray((schema as LooseObject).required)
      ? ((schema as LooseObject).required as string[])
      : [],
  );

  const fields: ConfigField[] = [];
  for (const [key, value] of Object.entries(properties)) {
    const field = value as LooseObject;
    if (!field || typeof field !== "object" || Array.isArray(field)) {
      continue;
    }
    const label = typeof field.title === "string" ? field.title : key;
    const description =
      typeof field.description === "string" ? field.description : undefined;
    const required = requiredKeys.has(key);

    if (secretFields.has(key) || field.secret === true) {
      fields.push({ key, label, description, type: "password", required });
      continue;
    }
    if (field.type === "boolean") {
      fields.push({ key, label, description, type: "checkbox", required });
      continue;
    }
    fields.push({ key, label, description, type: "text", required });
  }
  return fields;
}

function providerLabel(provider: ProviderHubInstallation): string {
  if (provider.name) {
    return provider.name;
  }
  const manifestName = (provider.manifest as LooseObject | undefined)?.name;
  return typeof manifestName === "string" ? manifestName : provider.provider_id;
}

export interface ProviderConfigureStageProps {
  onNext: () => void;
  onBack?: () => void;
  onInstallMore: () => void;
}

/**
 * Second Providers sub-stage. Lists installed providers with an enable toggle
 * and, once enabled, the credential fields derived from the provider manifest.
 * Continue is hard-gated until at least one provider is enabled, then persists
 * the enabled set plus each provider's per-field settings.
 */
const ProviderConfigureStage: FC<ProviderConfigureStageProps> = ({
  onNext,
  onBack,
  onInstallMore,
}) => {
  const { data: providers } = useProviderHubProviders();
  const settings = useSettingsMutation();

  const installed = useMemo(() => providers ?? [], [providers]);

  const [enabled, setEnabled] = useState<string[]>([]);
  // Keyed by `${providerId}::${fieldKey}` so different providers never collide.
  const [values, setValues] = useState<Record<string, string | boolean>>({});

  const toggleEnabled = (providerId: string) => {
    setEnabled((current) =>
      current.includes(providerId)
        ? current.filter((id) => id !== providerId)
        : [...current, providerId],
    );
  };

  const setValue = (
    providerId: string,
    fieldKey: string,
    value: string | boolean,
  ) => {
    setValues((current) => ({
      ...current,
      [`${providerId}::${fieldKey}`]: value,
    }));
  };

  const canContinue = enabled.length > 0;

  const handleContinue = () => {
    const payload: LooseObject = {
      "settings-general-enabled_providers": enabled,
    };

    for (const provider of installed) {
      const providerId = provider.provider_id;
      if (!enabled.includes(providerId)) {
        continue;
      }
      const fields = fieldsFromManifest(provider.manifest);
      for (const field of fields) {
        const value = values[`${providerId}::${field.key}`];
        if (value === undefined) {
          continue;
        }
        payload[`settings-${providerId}-${field.key}`] = value;
      }
    }

    settings.mutate(payload, {
      onSuccess: () => {
        onNext();
      },
    });
  };

  return (
    <Stack gap="lg">
      <Stack gap="xs">
        <Title order={2}>Enable and configure providers</Title>
        <Text c="dimmed">
          Turn on the providers you want to use and enter any credentials they
          need. You must enable at least one provider to continue.
        </Text>
      </Stack>

      <ScrollArea.Autosize mah={380} type="auto" offsetScrollbars>
        <Stack gap="md" pr="sm">
          {installed.map((provider, index) => {
            const providerId = provider.provider_id;
            const isEnabled = enabled.includes(providerId);
            const fields = essentialFields(
              fieldsFromManifest(provider.manifest),
            );
            return (
              <Stack key={providerId} gap="sm">
                {index > 0 && <Divider />}
                <Checkbox
                  label={providerLabel(provider)}
                  checked={isEnabled}
                  onChange={() => toggleEnabled(providerId)}
                />
                {isEnabled && fields.length === 0 && (
                  <Text size="sm" c="dimmed" pl="xl">
                    No credentials needed.
                  </Text>
                )}
                {isEnabled &&
                  fields.map((field) => {
                    const fieldValue = values[`${providerId}::${field.key}`];
                    if (field.type === "checkbox") {
                      return (
                        <Checkbox
                          key={field.key}
                          label={field.label}
                          description={field.description}
                          checked={fieldValue === true}
                          onChange={(event) =>
                            setValue(
                              providerId,
                              field.key,
                              event.currentTarget.checked,
                            )
                          }
                        />
                      );
                    }
                    if (field.type === "password") {
                      return (
                        <PasswordInput
                          key={field.key}
                          label={field.label}
                          description={field.description}
                          value={
                            typeof fieldValue === "string" ? fieldValue : ""
                          }
                          onChange={(event) =>
                            setValue(
                              providerId,
                              field.key,
                              event.currentTarget.value,
                            )
                          }
                        />
                      );
                    }
                    return (
                      <TextInput
                        key={field.key}
                        label={field.label}
                        description={field.description}
                        value={typeof fieldValue === "string" ? fieldValue : ""}
                        onChange={(event) =>
                          setValue(
                            providerId,
                            field.key,
                            event.currentTarget.value,
                          )
                        }
                      />
                    );
                  })}
              </Stack>
            );
          })}
        </Stack>
      </ScrollArea.Autosize>

      <Text size="xs" c="dimmed">
        Advanced provider options are available later in Settings, Providers.
      </Text>

      <Anchor component="button" type="button" onClick={onInstallMore}>
        Install more providers
      </Anchor>

      <Group justify="space-between">
        <Group gap="sm">
          {onBack && (
            <Button variant="default" onClick={onBack}>
              Back
            </Button>
          )}
        </Group>
        <Button
          onClick={handleContinue}
          loading={settings.isPending}
          disabled={!canContinue}
        >
          Continue
        </Button>
      </Group>
    </Stack>
  );
};

export default ProviderConfigureStage;
