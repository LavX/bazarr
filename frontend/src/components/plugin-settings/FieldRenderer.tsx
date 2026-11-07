import { FunctionComponent, ReactNode, useState } from "react";
import {
  Anchor,
  Button,
  Group,
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
import GroupWrapper from "./GroupWrapper";
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
  /** The stored value of this secret field will be removed when the form is saved. */
  pendingClear?: boolean;
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

/** The number a half-typed entry stands for: null when it is empty, NaN when it is not a number yet ("-"). */
function parseEntry(entry: string | number): number | null {
  if (typeof entry === "number") {
    return entry;
  }
  return entry.trim() === "" ? null : Number(entry);
}

interface NumberFieldProps {
  field: PluginField;
  label: string;
  description: ReactNode;
  /** What the form holds for this field: a number, null once emptied, undefined when never set. */
  stored: PluginValue | undefined;
  disabled?: boolean;
  error?: string;
  onValue: (value: number | null) => void;
}

/**
 * A number input that keeps what is being typed. "0." and "1." are steps towards 0.15 and 1.5, so the
 * entry stays as text while it is edited and only its value goes to the form: a number once there is
 * one, null (reset to the default) when the box is emptied. A lone "-" changes nothing yet.
 */
function NumberField({
  field,
  label,
  description,
  stored,
  disabled,
  error,
  onValue,
}: NumberFieldProps) {
  // What is being typed, and the value the form held when it was last changed from here.
  const [typed, setTyped] = useState<{
    entry: string | number;
    emitted: number | null;
  } | null>(null);
  const storedNumber = typeof stored === "number" ? stored : null;
  const parsed = typed === null ? null : parseEntry(typed.entry);
  // The entry is shown while it still agrees with the form. A complete number agrees with the same number; an
  // empty or half-typed entry ("", "-", "1e") agrees while the form still holds what it held when the typing
  // began, so replacing 40 with -5 keeps the "-" on screen. A preset or reset elsewhere replaces the entry.
  const agrees =
    typed !== null &&
    (parsed !== null && !Number.isNaN(parsed)
      ? parsed === storedNumber
      : storedNumber === typed.emitted);
  const shown = agrees
    ? typed.entry
    : storedNumber !== null
      ? storedNumber
      : stored === undefined && typeof field.default === "number"
        ? field.default
        : "";
  return (
    <NumberInput
      label={label}
      description={description}
      value={shown}
      placeholder={
        typeof field.default === "number" ? String(field.default) : undefined
      }
      min={field.min}
      max={field.max}
      allowDecimal={field.type === "number"}
      required={field.required}
      disabled={disabled}
      error={error}
      onChange={(next) => {
        const value = parseEntry(next);
        if (value === null) {
          setTyped({ entry: next, emitted: null });
          onValue(null);
        } else if (Number.isFinite(value)) {
          setTyped({ entry: next, emitted: value });
          onValue(value);
        } else {
          // Not a number yet: keep it on screen, change nothing in the form.
          setTyped({ entry: next, emitted: storedNumber });
        }
      }}
    />
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
  pendingClear,
}) => {
  const label = field.label ?? field.key;
  const description = <Help field={field} />;
  const current = values[field.key] ?? field.default;
  const set = (value: PluginValue | undefined) =>
    onChange({ ...values, [field.key]: value });

  // A text field the manifest marks secret is held like a secret field: masked, with its stored status and a
  // way to clear it. It must never be drawn as a plain text box.
  const kind =
    field.type === "text" && field.secret === true ? "secret" : field.type;
  switch (kind) {
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
        <Stack gap={4}>
          <PasswordInput
            label={label}
            description={description}
            placeholder={
              pendingClear
                ? "The saved value will be removed when you save"
                : secret?.configured
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
          {secret?.configured && onClearSecret && !pendingClear && (
            <Group>
              <Button
                size="compact-xs"
                variant="subtle"
                color="red"
                disabled={disabled}
                onClick={() => onClearSecret(field.key)}
              >
                {`Clear saved ${label}`}
              </Button>
            </Group>
          )}
        </Stack>
      );
    case "number":
    case "integer":
      return (
        <NumberField
          field={field}
          label={label}
          description={description}
          stored={values[field.key]}
          disabled={disabled}
          error={error}
          onValue={(next) => set(next)}
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
          <GroupWrapper
            label={label}
            description={field.help || field.help_url ? description : undefined}
            error={error}
            role="radiogroup"
          >
            <SegmentedControl
              data={data}
              value={asString(current)}
              disabled={disabled}
              onChange={(next) => onChange(applyChoice(field, next, values))}
            />
          </GroupWrapper>
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
          value={asString(current)}
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
          health={secret?.health}
          maxItems={field.max_items}
          disabled={disabled}
          error={error}
          pendingClear={pendingClear}
          onChange={(next) => set(next)}
          onClear={onClearSecret ? () => onClearSecret(field.key) : undefined}
        />
      );
    case "list":
      // A list of secrets is held like a key list: write-only, counted, cleared as a whole.
      if (field.secret === true) {
        return (
          <KeyListField
            label={label}
            description={field.help}
            value={asList(values[field.key])}
            stored={secret}
            health={secret?.health}
            maxItems={field.max_items}
            disabled={disabled}
            error={error}
            pendingClear={pendingClear}
            onChange={(next) => set(next)}
            onClear={onClearSecret ? () => onClearSecret(field.key) : undefined}
          />
        );
      }
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
        <GroupWrapper
          label={label}
          description={field.help || field.help_url ? description : undefined}
          error={error}
        >
          <SortableList
            items={order}
            getId={(value) => value}
            getName={labelOf}
            disabled={disabled}
            onReorder={(ids) => set(ids)}
            renderItem={(value) => <Text size="sm">{labelOf(value)}</Text>}
          />
        </GroupWrapper>
      );
    }
  }
};

export default FieldRenderer;
