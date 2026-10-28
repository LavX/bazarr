import { FC, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Anchor,
  Button,
  Checkbox,
  Group,
  PasswordInput,
  Stack,
  Text,
  TextInput,
} from "@mantine/core";
import { AxiosError } from "axios";
import {
  useProviderHubProviders,
  useSettingsMutation,
  useSystemSettings,
} from "@/apis/hooks";
import type {
  ProviderHubInstallation,
  ProviderHubManifest,
} from "@/apis/raw/providerHub";
import StepLayout from "@/pages/Setup/StepLayout";
import { useReportStepBusy } from "@/pages/Setup/useStepBusy";
import styles from "./ProviderGrid.module.scss";

type FieldType = "text" | "password" | "checkbox";

interface ConfigField {
  key: string;
  label: string;
  description?: string;
  type: FieldType;
  required: boolean;
}

// The half of a login that is not the secret: the account name a provider
// signs in with. Schemas mark these optional often enough (the provider can be
// queried anonymously, with credentials as an upgrade) that filtering on
// `required` alone rendered the password box on its own and asked for half a
// login.
const IDENTITY_KEY = /user|email|login|account/i;

// First-run shows only the fields a provider actually needs to start working:
// required fields, secret credentials, and the account name that goes with a
// secret. Advanced toggles (forced-only, FPS, FlareSolverr, delays,
// AI-translation flags, ...) stay hidden here and remain available later in
// Subtitle Hub > My Providers, so the step does not become an overwhelming
// wall of inputs.
function essentialFields(fields: ConfigField[]): ConfigField[] {
  const hasSecret = fields.some((field) => field.type === "password");
  return fields.filter(
    (field) =>
      field.required ||
      field.type === "password" ||
      (hasSecret && field.type === "text" && IDENTITY_KEY.test(field.key)),
  );
}

// Why the save did not happen, in the backend's own words where it gave any.
// The raw client swallows 502/503 and network errors, which is exactly what a
// backend still coming back from the install restart produces, so this line is
// the only thing that will say anything at all.
function describeSaveError(reason: unknown): string {
  if (reason instanceof AxiosError) {
    const data = reason.response?.data as { message?: string } | undefined;
    if (data?.message) {
      return data.message;
    }
  }
  if (reason instanceof Error && reason.message) {
    return reason.message;
  }
  return "Bazarr+ did not save the providers. It may still be restarting; wait a moment and try again.";
}

function isBlank(value: unknown): boolean {
  if (value === undefined || value === null) {
    return true;
  }
  return typeof value === "string" && value.trim().length === 0;
}

// What Bazarr+ already holds for this provider field. A provider configured
// before the wizard ran (or on an earlier pass through it) must not be
// reported as missing a credential it has.
function storedValue(
  settings: LooseObject | undefined,
  providerId: string,
  fieldKey: string,
): unknown {
  const section = settings?.[providerId];
  if (!section || typeof section !== "object" || Array.isArray(section)) {
    return undefined;
  }
  return (section as LooseObject)[fieldKey];
}

// The settings API never returns a plugin secret, so the section carries a
// descriptor instead: `__secret_fields` says whether each one is configured
// and, for a long one, how it ends. A shipped provider has no descriptor and
// this returns undefined.
interface SecretFieldDescriptor {
  configured?: boolean;
  last_four?: string;
  decrypt_failed?: boolean;
}
function secretDescriptor(
  settings: LooseObject | undefined,
  providerId: string,
  fieldKey: string,
): SecretFieldDescriptor | undefined {
  const section = settings?.[providerId];
  if (!section || typeof section !== "object" || Array.isArray(section)) {
    return undefined;
  }
  const descriptors = (section as LooseObject).__secret_fields;
  if (
    !descriptors ||
    typeof descriptors !== "object" ||
    Array.isArray(descriptors)
  ) {
    return undefined;
  }
  return (descriptors as LooseObject)[fieldKey.toLowerCase()] as
    | SecretFieldDescriptor
    | undefined;
}

// What the wizard says about a stored secret without ever reading it back:
// the configured text, or undefined. A credential that failed to decrypt is
// never reported as configured here; its own message goes beside the input,
// and the Continue gate demands a replacement.
function describeConfiguredSecret(
  descriptor: SecretFieldDescriptor | undefined,
): string | undefined {
  if (descriptor?.configured === true && descriptor.decrypt_failed !== true) {
    return descriptor.last_four
      ? `Configured, ends in ${descriptor.last_four}`
      : "Configured";
  }
  return undefined;
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
 *
 * The toggles start from the enabled providers Bazarr+ already has, rather than
 * from an empty set. "Install recommended" enables its providers as it installs
 * them, so this screen is where that has to show up, or the reader is asked to
 * answer the same question twice; and because Continue writes the whole list,
 * starting empty would silently turn off everything already on the moment they
 * ticked one provider by hand.
 */
const ProviderConfigureStage: FC<ProviderConfigureStageProps> = ({
  onNext,
  onBack,
  onInstallMore,
}) => {
  const {
    data: providers,
    isError: providersFailed,
    error: providersError,
    isFetching: providersFetching,
    refetch: refetchProviders,
  } = useProviderHubProviders();
  const { data: systemSettings } = useSystemSettings();
  const settings = useSettingsMutation(false, { inlineErrors: true });
  // A save in flight owns this step until it answers: the shell's skip would
  // advance the wizard while the write is still going, and the mutation's own
  // onSuccess would then advance it a second time and report a failure to a
  // stage nobody is rendering.
  useReportStepBusy(settings.isPending);

  const installed = useMemo(() => providers ?? [], [providers]);

  const [enabled, setEnabled] = useState<string[]>([]);
  // Until the reader touches a toggle, the toggles follow the truth: the
  // settings query is the same list the backend reads for a provider's enabled
  // state, and it is refreshed by the write that enabled the recommended set.
  // Once the reader has answered for themselves, their answer stands.
  const [answered, setAnswered] = useState(false);
  useEffect(() => {
    if (answered) {
      return;
    }
    setEnabled(systemSettings?.general?.enabled_providers ?? []);
  }, [answered, systemSettings]);
  // Keyed by `${providerId}::${fieldKey}` so different providers never collide.
  const [values, setValues] = useState<Record<string, string | boolean>>({});
  // A stored secret is replaced through an explicit action, so per field the
  // box stays read-only until Replace is pressed (Cancel discards the
  // staged replacement and locks the box again). Keyed like `values`.
  const [replacing, setReplacing] = useState<Record<string, boolean>>({});
  // Credentials are only marked once Continue has been pressed: every required
  // field would otherwise turn red the instant a provider is ticked, before
  // the reader has had a chance to type anything.
  const [checked, setChecked] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const toggleEnabled = (providerId: string) => {
    setAnswered(true);
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

  // Take a field back out of the staged answers, so it is submitted exactly
  // as untouched (absent means unchanged on the backend).
  const unsetValue = (providerId: string, fieldKey: string) => {
    setValues((current) => {
      const next = { ...current };
      delete next[`${providerId}::${fieldKey}`];
      return next;
    });
  };

  const beginReplace = (providerId: string, fieldKey: string) => {
    setReplacing((current) => ({
      ...current,
      [`${providerId}::${fieldKey}`]: true,
    }));
  };

  // Cancel returns the field to Configured: the box locks again and the
  // staged replacement, if any, goes away.
  const cancelReplace = (providerId: string, fieldKey: string) => {
    setReplacing((current) => ({
      ...current,
      [`${providerId}::${fieldKey}`]: false,
    }));
    unsetValue(providerId, fieldKey);
  };

  // Every enabled provider's required credentials that are still blank, both
  // typed and already stored. The stage used to compute the required set only
  // to decide what to render: a provider could be ticked, left empty and saved
  // enabled, and the reader met silent search failures instead of a message.
  const missing = useMemo(() => {
    const found: {
      providerId: string;
      label: string;
      fields: ConfigField[];
    }[] = [];
    for (const provider of installed) {
      const providerId = provider.provider_id;
      if (!enabled.includes(providerId)) {
        continue;
      }
      const blanks = essentialFields(fieldsFromManifest(provider.manifest))
        .filter((field) => field.required && field.type !== "checkbox")
        .filter((field) => {
          const typed = values[`${providerId}::${field.key}`];
          if (typed !== undefined) {
            return isBlank(typed);
          }
          // A stored secret never reads back as its value, so an empty box
          // does not mean it is missing: the descriptor says whether it is
          // there and usable. A credential that failed to decrypt is not
          // usable and is asked for again.
          if (field.type === "password") {
            const descriptor = secretDescriptor(
              systemSettings as LooseObject | undefined,
              providerId,
              field.key,
            );
            if (
              descriptor?.configured === true &&
              descriptor.decrypt_failed !== true
            ) {
              return false;
            }
          }
          return isBlank(
            storedValue(
              systemSettings as LooseObject | undefined,
              providerId,
              field.key,
            ),
          );
        });
      if (blanks.length > 0) {
        found.push({
          providerId,
          label: providerLabel(provider),
          fields: blanks,
        });
      }
    }
    return found;
  }, [enabled, installed, systemSettings, values]);

  const missingKeys = useMemo(
    () =>
      new Set(
        missing.flatMap((entry) =>
          entry.fields.map((field) => `${entry.providerId}::${field.key}`),
        ),
      ),
    [missing],
  );

  const canContinue = enabled.length > 0;

  const handleContinue = () => {
    setChecked(true);
    if (missing.length > 0) {
      return;
    }
    setSaveError(null);
    const payload: LooseObject = {
      "settings-general-enabled_providers": enabled,
    };
    const disabled = systemSettings?.general?.disabled_providers;
    if (disabled !== undefined) {
      payload["settings-general-disabled_providers"] = disabled.filter(
        (id) => !enabled.includes(id),
      );
    }

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
      onError: (reason) => setSaveError(describeSaveError(reason)),
    });
  };

  return (
    <StepLayout
      layout="wide"
      title="Enable and configure providers"
      description="Turn on the providers you want to use and enter any credentials they need."
      aside={
        <Stack gap={4}>
          <Text c="dimmed" size="sm">
            Until you enable a provider, Bazarr+ has nothing to search. You can
            do this from the Subtitle Hub whenever you like.
          </Text>
          <Text size="xs" c="dimmed">
            Advanced provider options are available later in Settings,
            Providers.
          </Text>
        </Stack>
      }
      stickyActions
      actions={
        // Install more sits beside Back rather than on a line of its own: it is
        // where the reader goes next if this list is not enough, which is what
        // the rest of this row is for, and two spare lines above the buttons
        // are two lines the list does not get.
        <Group justify="space-between">
          <Group gap="sm">
            {onBack && (
              <Button variant="default" onClick={onBack}>
                Back
              </Button>
            )}
            <Anchor component="button" type="button" onClick={onInstallMore}>
              Install more providers
            </Anchor>
          </Group>
          <Button
            onClick={handleContinue}
            loading={settings.isPending}
            disabled={!canContinue}
          >
            Continue
          </Button>
        </Group>
      }
    >
      {/* A list that failed to load is not a list of nothing. Without this the
          step showed a heading, a rule that at least one provider must be
          enabled, an empty region and a dead Continue, on a step that cannot
          be skipped. */}
      {providersFailed && (
        <Alert color="red" title="Could not load the installed providers">
          <Stack gap="sm" align="flex-start">
            <Text size="sm">{describeSaveError(providersError)}</Text>
            <Button
              variant="default"
              size="xs"
              loading={providersFetching}
              onClick={() => void refetchProviders()}
            >
              Retry
            </Button>
          </Stack>
        </Alert>
      )}

      <div className={styles.grid}>
        {installed.map((provider) => {
          const providerId = provider.provider_id;
          const isEnabled = enabled.includes(providerId);
          const fields = essentialFields(fieldsFromManifest(provider.manifest));
          // What this provider will ask for, said before it is ticked and in
          // one line. The list is three dozen entries on a real install, so a
          // provider that needs nothing says nothing at all: "No credentials
          // needed" under every row was a second row of nothing, thirty-six
          // times over, and it pushed the buttons off the screen.
          const needs = fields
            .filter((field) => field.type !== "checkbox")
            .map((field) => field.label)
            .join(", ");
          return (
            <div key={providerId} className={styles.cell}>
              <Checkbox
                className={styles.entry}
                label={providerLabel(provider)}
                checked={isEnabled}
                onChange={() => toggleEnabled(providerId)}
              />
              {!isEnabled && needs.length > 0 && (
                <Text
                  size="xs"
                  c="dimmed"
                  className={styles.note}
                  title={`Needs ${needs}`}
                >
                  Needs {needs}
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
                  const fieldError =
                    checked && missingKeys.has(`${providerId}::${field.key}`)
                      ? `${providerLabel(provider)} needs this to search`
                      : undefined;
                  if (field.type === "password") {
                    // The stored secret never reads back, so the empty box
                    // carries the descriptor's word for it. Every action is
                    // explicit, so continuing never wipes or keeps a key by
                    // accident: untouched keeps the stored secret, only the
                    // Clear button stages the removal (Undo takes it back),
                    // a replacement needs Replace pressed first and Cancel
                    // returns to Configured, and emptying the input by
                    // typing unstages the field again.
                    const descriptor = secretDescriptor(
                      systemSettings as LooseObject | undefined,
                      providerId,
                      field.key,
                    );
                    // Only the truly empty value is the staged clear. A
                    // spaces-only input is never a credential, so it never
                    // reaches the staged answers (the input treats it as
                    // blank), and saying it will clear the key while saving
                    // the spaces would make the two forms disagree.
                    const clearStaged =
                      fieldValue !== undefined &&
                      (fieldValue === "" || fieldValue === null);
                    const replaceStaged =
                      typeof fieldValue === "string" && fieldValue.length > 0;
                    const decryptFailed = descriptor?.decrypt_failed === true;
                    const locked =
                      descriptor?.configured === true || decryptFailed;
                    const replacingThis =
                      replacing[`${providerId}::${field.key}`] === true;
                    // The staged state is said first, because it is the
                    // action the reader is in the middle of; the decrypt
                    // failure shows only when nothing is staged, so a
                    // replacement typed for a failed credential is still
                    // said to replace the stored key.
                    const helper: string[] = [];
                    if (field.description) {
                      helper.push(field.description);
                    }
                    if (clearStaged) {
                      helper.push("Will be cleared when you continue.");
                    } else if (replaceStaged) {
                      helper.push("Replaces the stored key when you continue.");
                    } else {
                      const configuredText =
                        describeConfiguredSecret(descriptor);
                      if (configuredText) {
                        helper.push(configuredText);
                      }
                    }
                    if (decryptFailed && !clearStaged && !replaceStaged) {
                      helper.push(
                        "The saved value could not be decrypted. Re-enter it and save.",
                      );
                    }
                    return (
                      <div key={field.key}>
                        <PasswordInput
                          size="xs"
                          label={field.label}
                          description={
                            helper.length > 0 ? helper.join(" ") : undefined
                          }
                          error={fieldError}
                          disabled={locked && !replacingThis}
                          value={
                            typeof fieldValue === "string" ? fieldValue : ""
                          }
                          onChange={(event) => {
                            const next = event.currentTarget.value;
                            if (isBlank(next)) {
                              // An empty or spaces-only input is editing,
                              // not clearing: the staged answer goes away
                              // and the stored secret stands. A credential
                              // made only of spaces is never what a reader
                              // meant to save.
                              unsetValue(providerId, field.key);
                            } else {
                              setValue(providerId, field.key, next);
                            }
                          }}
                        />
                        {locked &&
                          !replacingThis &&
                          !clearStaged &&
                          !replaceStaged && (
                            <Button
                              size="compact-xs"
                              variant="subtle"
                              onClick={() =>
                                beginReplace(providerId, field.key)
                              }
                            >
                              Replace
                            </Button>
                          )}
                        {(replacingThis || replaceStaged) && !clearStaged && (
                          <Button
                            size="compact-xs"
                            variant="subtle"
                            onClick={() => cancelReplace(providerId, field.key)}
                          >
                            Cancel
                          </Button>
                        )}
                        {clearStaged && (
                          <Button
                            size="compact-xs"
                            variant="subtle"
                            onClick={() => unsetValue(providerId, field.key)}
                          >
                            Undo
                          </Button>
                        )}
                        {descriptor?.configured === true &&
                          fieldValue === undefined && (
                            <Button
                              size="compact-xs"
                              variant="subtle"
                              onClick={() =>
                                setValue(providerId, field.key, "")
                              }
                            >
                              Clear
                            </Button>
                          )}
                      </div>
                    );
                  }
                  return (
                    <TextInput
                      key={field.key}
                      size="xs"
                      label={field.label}
                      description={field.description}
                      error={fieldError}
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
            </div>
          );
        })}
      </div>

      {checked && missing.length > 0 && (
        <Alert color="red" title="Missing credentials">
          {missing
            .map(
              (entry) =>
                `${entry.label} needs ${entry.fields
                  .map((field) => field.label)
                  .join(", ")}`,
            )
            .join(". ")}
          . Fill them in, or turn those providers off.
        </Alert>
      )}
      {saveError && (
        <Alert color="red" title="Could not save the providers">
          {saveError}
        </Alert>
      )}
    </StepLayout>
  );
};

export default ProviderConfigureStage;
