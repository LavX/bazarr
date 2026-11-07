import { FunctionComponent, ReactNode } from "react";
import {
  Anchor,
  NumberInput,
  PasswordInput,
  SegmentedControl,
  Select,
  Stack,
  Switch,
  TagsInput,
  Text,
  TextInput,
} from "@mantine/core";
import { applyChoice, choiceOf, itemOf } from "./fields";
import KeyListField from "./KeyListField";
import ModelPicker from "./ModelPicker";
import SortableList from "./SortableList";
import type {
  OptionsState,
  PluginField,
  PluginSecretState,
  PluginValue,
  PluginValues,
} from "./types";

export interface FieldRendererProps {
  field: PluginField;
  /** Every value of the form, defaults not applied. */
  values: PluginValues;
  onChange: (values: PluginValues) => void;
  secret?: PluginSecretState;
  error?: string;
  disabled?: boolean;
  /** The live choices of an `options` field. Without them the field is free text. */
  options?: OptionsState;
  /** Called with a secret field name when the user clears what is stored. */
  onClearSecret?: (key: string) => void;
}

// An address on the loopback interface reaches the Bazarr+ container, not the host it runs on.
export function pointsAtThisContainer(value: string): boolean {
  try {
    const host = new URL(value).hostname;
    return host === "localhost" || host === "127.0.0.1" || host === "[::1]";
  } catch {
    return false;
  }
}

const NO_OPTIONS: OptionsState = { available: false, items: [] };

const asString = (value: PluginValue | undefined) =>
  typeof value === "string" ? value : "";

const asList = (value: PluginValue | undefined) =>
  Array.isArray(value) ? value : [];

function Help({ field }: { field: PluginField }): ReactNode {
  if (!field.help && !field.help_url) {
    return undefined;
  }
  return (
    <>
      {field.help}
      {field.help_url && (
        <>
          {" "}
          <Anchor
            href={field.help_url}
            target="_blank"
            rel="noopener noreferrer"
            size="xs"
          >
            Learn more
          </Anchor>
        </>
      )}
    </>
  );
}

/**
 * One input for one manifest field, picked by the field's type alone. It has no
 * idea what the field is for: a name, a price and a base URL all go through here.
 */
const FieldRenderer: FunctionComponent<FieldRendererProps> = ({
  field,
  values,
  onChange,
  secret,
  error,
  disabled,
  options,
  onClearSecret,
}) => {
  const label = field.label ?? field.key;
  const description = <Help field={field} />;
  const current = values[field.key] ?? field.default;
  const set = (value: PluginValue | undefined) =>
    onChange({ ...values, [field.key]: value });

  switch (field.type) {
    case "text":
      return (
        <TextInput
          label={label}
          description={
            field.destination && pointsAtThisContainer(asString(current)) ? (
              <>
                {description} <b>localhost</b> inside the container is the
                container itself. Use <code>host.docker.internal</code> or the
                server&apos;s address on your network.
              </>
            ) : (
              description
            )
          }
          placeholder={field.placeholder}
          value={asString(current)}
          maxLength={field.max_length}
          required={field.required}
          disabled={disabled}
          error={error}
          onChange={(event) => set(event.currentTarget.value)}
        />
      );
    case "secret":
      return (
        <PasswordInput
          label={label}
          description={description}
          placeholder={
            secret?.configured
              ? secret.stale
                ? "Enter the key again for this address"
                : secret.last4
                  ? `Saved, ending ${secret.last4}. Leave empty to keep it.`
                  : "Saved. Leave empty to keep it."
              : field.placeholder
          }
          autoComplete="off"
          value={asString(values[field.key])}
          required={field.required && !secret?.configured}
          disabled={disabled}
          error={error}
          onChange={(event) => set(event.currentTarget.value)}
        />
      );
    case "number":
    case "integer":
      return (
        <NumberInput
          label={label}
          description={description}
          value={typeof current === "number" ? current : ""}
          min={field.min}
          max={field.max}
          allowDecimal={field.type === "number"}
          required={field.required}
          disabled={disabled}
          error={error}
          onChange={(next) => set(typeof next === "number" ? next : undefined)}
        />
      );
    case "toggle":
      return (
        <Switch
          label={label}
          description={description}
          checked={current === true}
          disabled={disabled}
          error={error}
          onChange={(event) => set(event.currentTarget.checked)}
        />
      );
    case "choice": {
      const choices = (field.choices ?? []).map(choiceOf);
      const data = choices.map((item) => ({
        value: item.value,
        label: item.label ?? item.value,
      }));
      if (data.length <= 4) {
        return (
          <Stack gap={4}>
            <Text size="sm" fw={500}>
              {label}
            </Text>
            <SegmentedControl
              data={data}
              value={asString(current)}
              disabled={disabled}
              aria-label={label}
              onChange={(next) => onChange(applyChoice(field, next, values))}
            />
            {field.help && (
              <Text size="xs" c="dimmed">
                {description}
              </Text>
            )}
            {error && (
              <Text size="xs" c="red">
                {error}
              </Text>
            )}
          </Stack>
        );
      }
      return (
        <Select
          label={label}
          description={description}
          data={data}
          value={asString(current) || null}
          allowDeselect={false}
          disabled={disabled}
          error={error}
          onChange={(next) =>
            next !== null && onChange(applyChoice(field, next, values))
          }
        />
      );
    }
    case "options":
      return (
        <ModelPicker
          label={label}
          description={field.help}
          placeholder={field.placeholder}
          value={asString(values[field.key])}
          options={options ?? NO_OPTIONS}
          disabled={disabled}
          error={error}
          onChange={(next) => set(next)}
          onPick={(model) =>
            onChange({
              ...values,
              ...(model.sets ?? {}),
              [field.key]: model.id,
            })
          }
        />
      );
    case "key_list":
      return (
        <KeyListField
          label={label}
          description={field.help}
          value={asList(values[field.key])}
          stored={secret}
          maxItems={field.max_items}
          disabled={disabled}
          error={error}
          onChange={(next) => set(next)}
          onClear={() => onClearSecret?.(field.key)}
        />
      );
    case "list":
      return (
        <TagsInput
          label={label}
          description={description}
          placeholder={field.placeholder}
          value={asList(current)}
          maxTags={field.max_items}
          disabled={disabled}
          error={error}
          onChange={(next) => set(next)}
        />
      );
    case "orderable_list": {
      const known = (field.items ?? []).map(itemOf);
      const order = asList(current).length
        ? asList(current)
        : known.map((item) => item.value);
      const labelOf = (value: string) =>
        known.find((item) => item.value === value)?.label ?? value;
      return (
        <Stack gap={6}>
          <Text size="sm" fw={500}>
            {label}
          </Text>
          {field.help && (
            <Text size="xs" c="dimmed">
              {description}
            </Text>
          )}
          <SortableList
            items={order}
            getId={(value) => value}
            getName={labelOf}
            disabled={disabled}
            onReorder={(ids) => set(ids)}
            renderItem={(value) => <Text size="sm">{labelOf(value)}</Text>}
          />
          {error && (
            <Text size="xs" c="red">
              {error}
            </Text>
          )}
        </Stack>
      );
    }
  }
};

export default FieldRenderer;
