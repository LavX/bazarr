/* eslint-disable camelcase -- transport names. */
import { FunctionComponent, useState } from "react";
import {
  ActionIcon,
  Badge,
  Button,
  Group,
  Text,
  TextInput,
} from "@mantine/core";
import { faPlus, faXmark } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import GroupWrapper from "./GroupWrapper";
import type { KeyHealth, PluginSecretState } from "./types";

export interface KeyListFieldProps {
  label: string;
  description?: string;
  /** The keys typed in this form. Saving them replaces every stored key. */
  value: string[];
  /** What the server says it holds. It never says what the keys are. */
  stored?: PluginSecretState;
  /** Per-key health, by position, when the engine reports it. */
  health?: KeyHealth[];
  maxItems?: number;
  disabled?: boolean;
  error?: string;
  onChange: (value: string[]) => void;
  onClear?: () => void;
  /** The saved keys will be removed when the form is saved. */
  pendingClear?: boolean;
}

const HEALTH_COLOR = { ok: "green", cooling_down: "yellow", failed: "red" };

// A key shows its last four characters and nothing else once it is in the list.
const masked = (key: string) =>
  key.length > 8 ? `••••${key.slice(-4)}` : "••••";

const KeyListField: FunctionComponent<KeyListFieldProps> = ({
  label,
  description,
  value,
  stored,
  health,
  maxItems,
  disabled,
  error,
  onChange,
  onClear,
  pendingClear,
}) => {
  const [draft, setDraft] = useState("");
  const full = maxItems !== undefined && value.length >= maxItems;
  const add = () => {
    const key = draft.trim();
    if (!key || full || value.includes(key)) {
      return;
    }
    onChange([...value, key]);
    setDraft("");
  };
  return (
    <GroupWrapper label={label} description={description} error={error}>
      {pendingClear && (
        <Badge color="red" variant="light">
          Saved keys will be removed when you save
        </Badge>
      )}
      {stored?.configured && !pendingClear && (
        <Group gap="xs">
          <Badge color={stored.stale ? "yellow" : "green"} variant="light">
            {stored.stale
              ? "Saved keys do not apply to this address"
              : stored.count !== undefined
                ? `${stored.count} saved ${stored.count === 1 ? "key" : "keys"}`
                : "Keys saved"}
          </Badge>
          {onClear && (
            <Button
              size="compact-xs"
              variant="subtle"
              color="red"
              disabled={disabled}
              onClick={onClear}
            >
              Clear saved keys
            </Button>
          )}
        </Group>
      )}
      {value.map((key, index) => (
        <Group key={key} gap="xs" wrap="nowrap">
          <Text size="sm" ff="monospace" style={{ flex: 1 }}>
            {masked(key)}
          </Text>
          {health?.[index] && (
            <Badge
              color={HEALTH_COLOR[health[index].state]}
              variant="dot"
              title={health[index].detail}
            >
              {health[index].state.replace("_", " ")}
            </Badge>
          )}
          <ActionIcon
            variant="subtle"
            color="gray"
            aria-label={`Remove key ending ${key.slice(-4)}`}
            disabled={disabled}
            onClick={() => onChange(value.filter((item) => item !== key))}
          >
            <FontAwesomeIcon icon={faXmark} />
          </ActionIcon>
        </Group>
      ))}
      <Group gap="xs" wrap="nowrap" align="flex-end">
        <TextInput
          style={{ flex: 1 }}
          type="password"
          autoComplete="off"
          aria-label={`Add to ${label}`}
          placeholder={
            stored?.configured
              ? "Type keys to replace the saved ones"
              : "Add a key"
          }
          value={draft}
          disabled={disabled || full}
          aria-invalid={error ? true : undefined}
          onChange={(event) => setDraft(event.currentTarget.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              add();
            }
          }}
        />
        <Button
          variant="light"
          leftSection={<FontAwesomeIcon icon={faPlus} />}
          disabled={disabled || full || draft.trim() === ""}
          onClick={add}
        >
          Add
        </Button>
      </Group>
    </GroupWrapper>
  );
};

export default KeyListField;
