import { FunctionComponent } from "react";
import {
  Divider,
  Group,
  NumberInput,
  SegmentedControl,
  Select,
  Stack,
  Switch,
  TagsInput,
  Text,
} from "@mantine/core";
import type { ArrSportsSettings } from "@/apis/raw/arrInstances";
import {
  dayOptions,
  diskUpdateOptions,
  seriesSyncOptions,
  upgradeOptions,
} from "@/pages/Settings/Scheduler/options";
import {
  isSportsOverridden,
  setSportsOverride,
  SPORTS_OVERRIDE_FIELDS,
  sportsOverrideDefault,
  SportsOverrideField,
} from "./sportsOverrides";

interface ControlProps {
  field: SportsOverrideField;
  value: unknown;
  onChange: (value: unknown) => void;
}

const OverrideControl: FunctionComponent<ControlProps> = ({
  field,
  value,
  onChange,
}) => {
  switch (field.kind) {
    case "bool":
      return (
        <SegmentedControl
          size="xs"
          aria-label={field.label}
          value={value ? "on" : "off"}
          onChange={(next) => onChange(next === "on")}
          data={[
            { value: "on", label: "Enabled" },
            { value: "off", label: "Disabled" },
          ]}
        />
      );
    case "percent":
      return (
        <NumberInput
          size="xs"
          aria-label={field.label}
          w={110}
          min={1}
          max={100}
          allowDecimal={false}
          value={typeof value === "number" ? value : 70}
          onChange={(next) => onChange(Number(next))}
        />
      );
    case "tags":
      return (
        <TagsInput
          size="xs"
          aria-label={field.label}
          w={260}
          value={Array.isArray(value) ? (value as string[]) : []}
          onChange={(next) => onChange(next)}
        />
      );
    case "fullUpdate":
      return (
        <Select
          size="xs"
          aria-label={field.label}
          w={140}
          allowDeselect={false}
          data={diskUpdateOptions.map((option) => ({
            label: option.label,
            value: option.value,
          }))}
          value={typeof value === "string" ? value : "Daily"}
          onChange={(next) => onChange(next ?? "Daily")}
        />
      );
    case "syncInterval":
      return (
        <Select
          size="xs"
          aria-label={field.label}
          w={140}
          allowDeselect={false}
          data={seriesSyncOptions.map((option) => ({
            label: option.label,
            value: String(option.value),
          }))}
          value={String(typeof value === "number" ? value : 60)}
          onChange={(next) => onChange(Number(next))}
        />
      );
    case "searchFrequency":
      return (
        <Select
          size="xs"
          aria-label={field.label}
          w={140}
          allowDeselect={false}
          data={upgradeOptions.map((option) => ({
            label: option.label,
            value: String(option.value),
          }))}
          value={String(typeof value === "number" ? value : 6)}
          onChange={(next) => onChange(Number(next))}
        />
      );
    case "day":
      return (
        <Select
          size="xs"
          aria-label={field.label}
          w={140}
          allowDeselect={false}
          data={dayOptions.map((option) => ({
            label: option.label,
            value: String(option.value),
          }))}
          value={String(typeof value === "number" ? value : 6)}
          onChange={(next) => onChange(Number(next))}
        />
      );
    case "hour":
      return (
        <NumberInput
          size="xs"
          aria-label={field.label}
          w={110}
          min={0}
          max={23}
          allowDecimal={false}
          value={typeof value === "number" ? value : 4}
          onChange={(next) => onChange(Number(next))}
        />
      );
    default:
      return null;
  }
};

interface Props {
  value: ArrSportsSettings;
  onChange: (value: ArrSportsSettings) => void;
}

// Optional per-instance overrides for the global Sportarr settings. Each row
// inherits until its switch is turned on, at which point the blob carries the
// key and this instance stops tracking the global value for it.
const SportsSettings: FunctionComponent<Props> = ({ value, onChange }) => {
  return (
    <Stack gap="xs">
      <Divider label="Sportarr settings (optional)" labelPosition="left" />
      <Text size="xs" c="dimmed">
        Every setting below inherits from Connections and Scheduler settings.
        Override one only when this server needs to differ from the others.
      </Text>
      {SPORTS_OVERRIDE_FIELDS.map((field) => {
        const overridden = isSportsOverridden(value, field.key);
        return (
          <Group key={field.key} justify="space-between" wrap="nowrap">
            <Switch
              size="xs"
              label={field.label}
              checked={overridden}
              onChange={(event) =>
                onChange(
                  setSportsOverride(
                    value,
                    field.key,
                    event.currentTarget.checked
                      ? sportsOverrideDefault(field.kind)
                      : undefined,
                  ),
                )
              }
            />
            {overridden ? (
              <OverrideControl
                field={field}
                value={value[field.key]}
                onChange={(next) =>
                  onChange(setSportsOverride(value, field.key, next))
                }
              />
            ) : (
              <Text size="xs" c="dimmed">
                Inherited
              </Text>
            )}
          </Group>
        );
      })}
    </Stack>
  );
};

export default SportsSettings;
