import { FunctionComponent } from "react";
import { Stack, Text } from "@mantine/core";
import FieldRenderer from "./FieldRenderer";
import { groupFields, isVisible, withDefaults } from "./fields";
import type {
  OptionsState,
  PluginField,
  PluginSecrets,
  PluginValues,
} from "./types";

export interface PluginSettingsFormProps {
  fields: PluginField[];
  values: PluginValues;
  onChange: (values: PluginValues) => void;
  secrets?: PluginSecrets;
  /** Field errors by key, as the typed save returns them. */
  errors?: Record<string, string>;
  disabled?: boolean;
  optionsFor?: (key: string) => OptionsState | undefined;
  onClearSecret?: (key: string) => void;
  /** Secret fields whose stored value will be removed when the form is saved. */
  pendingClear?: string[];
}

/**
 * The form a manifest describes: its fields in groups, the ones whose
 * condition fails left out, each drawn by its type. Controlled: the caller owns
 * the values, so a preset that fills other fields is just a new values object.
 */
const PluginSettingsForm: FunctionComponent<PluginSettingsFormProps> = ({
  fields,
  values,
  onChange,
  secrets,
  errors,
  disabled,
  optionsFor,
  onClearSecret,
  pendingClear,
}) => {
  const shown = withDefaults(fields, values);
  const visible = fields.filter((field) => isVisible(field, shown));
  return (
    <Stack gap="md">
      {groupFields(visible).map(({ group, fields: inGroup }) => (
        <Stack key={group || "ungrouped"} gap="sm">
          {group && (
            <Text fw={600} size="sm">
              {group}
            </Text>
          )}
          {inGroup.map((field) => (
            <FieldRenderer
              key={field.key}
              field={field}
              values={values}
              onChange={onChange}
              secret={secrets?.[field.key]}
              error={errors?.[field.key]}
              disabled={disabled}
              options={optionsFor?.(field.key)}
              onClearSecret={onClearSecret}
              pendingClear={pendingClear?.includes(field.key)}
            />
          ))}
        </Stack>
      ))}
    </Stack>
  );
};

export default PluginSettingsForm;
