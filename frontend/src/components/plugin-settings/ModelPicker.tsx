import { FunctionComponent, useMemo } from "react";
import { Group, Select, SelectProps, Text, TextInput } from "@mantine/core";
import type { ModelOption, OptionsState } from "./types";

export interface ModelPickerProps {
  label: string;
  description?: string;
  placeholder?: string;
  value: string;
  options: OptionsState;
  disabled?: boolean;
  error?: string;
  onChange: (value: string) => void;
  /** Called when a model is picked from the list, so its prices can follow. */
  onPick?: (model: ModelOption) => void;
}

/** Only models that return text can translate subtitles. A list that does not say keeps every entry. */
export const textOutputModels = (items: ModelOption[]) =>
  items.filter(
    (item) =>
      item.output_modalities === undefined ||
      item.output_modalities.includes("text"),
  );

const price = (value: number | undefined) =>
  value === undefined
    ? "price unknown"
    : value === 0
      ? "free"
      : `$${value.toFixed(value < 1 ? 3 : 2)}/M`;

const context = (length: number | undefined) =>
  length === undefined
    ? ""
    : length >= 1_000_000
      ? `${(length / 1_000_000).toFixed(1)}M context`
      : `${Math.round(length / 1000)}k context`;

const ModelPicker: FunctionComponent<ModelPickerProps> = ({
  label,
  description,
  placeholder,
  value,
  options,
  disabled,
  error,
  onChange,
  onPick,
}) => {
  const models = useMemo(
    () => textOutputModels(options.items),
    [options.items],
  );
  const data = useMemo(() => {
    const listed = models.map((item) => ({
      value: item.id,
      label: item.name ?? item.id,
    }));
    // A model saved earlier that the list no longer offers stays selected.
    return value && !listed.some((item) => item.value === value)
      ? [{ value, label: value }, ...listed]
      : listed;
  }, [models, value]);

  // The list could not be fetched, or there is none to fetch: free text, so a
  // model the user knows still works.
  if (!options.available) {
    return (
      <TextInput
        label={label}
        description={
          description ??
          (options.loading
            ? "Loading the model list."
            : "The model list could not be fetched. Type the model name.")
        }
        placeholder={placeholder}
        value={value}
        disabled={disabled}
        error={error}
        onChange={(event) => onChange(event.currentTarget.value)}
      />
    );
  }

  const renderOption: SelectProps["renderOption"] = ({ option }) => {
    const model = models.find((item) => item.id === option.value);
    return (
      <Group justify="space-between" wrap="nowrap" w="100%" gap="md">
        <Text size="sm" truncate>
          {option.label}
        </Text>
        {model && (
          <Text size="xs" c="dimmed" style={{ whiteSpace: "nowrap" }}>
            {price(model.price_input_per_million)} in,{" "}
            {price(model.price_output_per_million)} out
            {model.context_length ? `, ${context(model.context_length)}` : ""}
          </Text>
        )}
      </Group>
    );
  };

  return (
    <Select
      label={label}
      description={description}
      placeholder={placeholder ?? "Pick a model"}
      searchable
      allowDeselect={false}
      data={data}
      value={value || null}
      disabled={disabled}
      error={error}
      renderOption={renderOption}
      onChange={(next) => {
        if (next === null) {
          return;
        }
        onChange(next);
        const model = models.find((item) => item.id === next);
        if (model) {
          onPick?.(model);
        }
      }}
    />
  );
};

export default ModelPicker;
