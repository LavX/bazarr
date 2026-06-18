import { FunctionComponent } from "react";
import {
  Alert,
  Divider,
  Group,
  MultiSelect,
  NumberInput,
  SegmentedControl,
  Select,
  Stack,
  Switch,
  Text,
  TextInput,
} from "@mantine/core";
import { faCircleInfo } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import type { ArrSubtitleSettings } from "@/apis/raw/arrInstances";
import { SUBTITLE_TOOL_ACTIONS } from "@/constants/batch";
import {
  syncEngineOptions,
  syncMaxOffsetSecondsOptions,
} from "@/pages/Settings/Subtitles/options";
import {
  isOverridden,
  OVERRIDE_FIELDS,
  overrideDefault,
  OverrideField,
  OverrideSection,
  setOverride,
} from "./subtitleOverrides";

const SECTION_LABELS: Record<OverrideSection, string> = {
  general: "Post-processing & modifications",
  subsync: "Audio synchronization",
};

const modOptions = SUBTITLE_TOOL_ACTIONS.map(([value, label]) => ({
  value,
  label,
}));

const offsetOptions = syncMaxOffsetSecondsOptions.map((option) => ({
  value: String(option.value),
  label: option.label,
}));

interface ControlProps {
  field: OverrideField;
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
          w={110}
          min={0}
          max={100}
          allowDecimal={false}
          value={typeof value === "number" ? value : 0}
          onChange={(next) =>
            onChange(typeof next === "number" ? next : Number(next) || 0)
          }
        />
      );
    case "text":
      return (
        <TextInput
          size="xs"
          style={{ flex: 1, minWidth: 220 }}
          placeholder="/path/to/script.sh"
          value={typeof value === "string" ? value : ""}
          onChange={(event) => onChange(event.currentTarget.value)}
        />
      );
    case "engines":
      return (
        <MultiSelect
          size="xs"
          style={{ flex: 1, minWidth: 220 }}
          data={syncEngineOptions}
          placeholder="Select engines"
          value={Array.isArray(value) ? (value as string[]) : []}
          onChange={(next) => onChange(next)}
        />
      );
    case "mods":
      return (
        <MultiSelect
          size="xs"
          style={{ flex: 1, minWidth: 220 }}
          data={modOptions}
          placeholder="Select modifications"
          value={Array.isArray(value) ? (value as string[]) : []}
          onChange={(next) => onChange(next)}
        />
      );
    case "offset":
      return (
        <Select
          size="xs"
          w={110}
          data={offsetOptions}
          allowDeselect={false}
          value={value !== undefined ? String(value) : null}
          onChange={(next) => onChange(next ? Number(next) : undefined)}
        />
      );
    default:
      return null;
  }
};

interface RowProps {
  field: OverrideField;
  blob: ArrSubtitleSettings;
  onChange: (next: ArrSubtitleSettings) => void;
}

const OverrideRow: FunctionComponent<RowProps> = ({
  field,
  blob,
  onChange,
}) => {
  const overridden = isOverridden(blob, field.section, field.key);
  const section = blob[field.section] as Record<string, unknown> | undefined;
  const current = overridden ? section?.[field.key] : undefined;

  const toggle = (checked: boolean) =>
    onChange(
      setOverride(
        blob,
        field.section,
        field.key,
        checked ? overrideDefault(field.kind) : undefined,
      ),
    );

  const setValue = (next: unknown) =>
    onChange(setOverride(blob, field.section, field.key, next));

  return (
    <Group justify="space-between" align="center" wrap="nowrap" gap="md">
      <Switch
        size="sm"
        checked={overridden}
        onChange={(event) => toggle(event.currentTarget.checked)}
        label={field.label}
        styles={{ label: { fontWeight: 500 } }}
      />
      {overridden ? (
        <OverrideControl field={field} value={current} onChange={setValue} />
      ) : (
        <Text size="xs" c="dimmed">
          Inherits global
        </Text>
      )}
    </Group>
  );
};

interface Props {
  value: ArrSubtitleSettings;
  onChange: (next: ArrSubtitleSettings) => void;
}

const SubtitleSettingsOverrides: FunctionComponent<Props> = ({
  value,
  onChange,
}) => {
  const sections: OverrideSection[] = ["general", "subsync"];
  return (
    <Stack gap="sm">
      <Alert
        variant="light"
        color="blue"
        icon={<FontAwesomeIcon icon={faCircleInfo} />}
      >
        Override only the settings that should differ for this instance.
        Everything left off inherits the global Subtitles settings.
      </Alert>
      {sections.map((section) => (
        <Stack key={section} gap="xs">
          <Divider label={SECTION_LABELS[section]} labelPosition="left" />
          {OVERRIDE_FIELDS.filter((field) => field.section === section).map(
            (field) => (
              <OverrideRow
                key={field.key}
                field={field}
                blob={value}
                onChange={onChange}
              />
            ),
          )}
        </Stack>
      ))}
    </Stack>
  );
};

export default SubtitleSettingsOverrides;
