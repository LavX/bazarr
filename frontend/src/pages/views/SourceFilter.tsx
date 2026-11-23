import { Select } from "@mantine/core";

interface Props {
  labels: Map<MediaSourceKey, string>;
  value: MediaSourceKey | readonly MediaSourceKey[] | null;
  onChange: (value: string | null) => void;
  includeSources?: boolean;
  showSingle?: boolean;
}

const multipleSelection = "multiple";

export default function SourceFilter({
  labels,
  value,
  onChange,
  includeSources = true,
  showSingle = false,
}: Props) {
  const selected = typeof value === "string" ? [value] : (value ?? []);
  if (labels.size <= (showSingle ? 0 : 1) && !selected.length) return null;

  // History can restore several keys from the URL. Keep its single-choice
  // control usable to replace or clear that scope, and show every active key.
  const summary =
    selected.length > 1
      ? selected.map((key) => labels.get(key) ?? key).join(", ")
      : undefined;
  return (
    <Select
      aria-label={includeSources ? "Source" : "Instance"}
      placeholder={includeSources ? "All sources" : "All instances"}
      data={[
        ...(summary
          ? [{ value: multipleSelection, label: summary, disabled: true }]
          : []),
        ...Array.from(labels, ([value, label]) => ({ value, label })),
      ]}
      title={summary}
      value={summary ? multipleSelection : (selected[0] ?? null)}
      onChange={(next) => {
        if (next !== multipleSelection) onChange(next);
      }}
      allowDeselect
      searchable
      clearable
      clearButtonProps={{
        "aria-hidden": false,
        "aria-label": includeSources
          ? "Clear source filter"
          : "Clear instance filter",
        tabIndex: 0,
      }}
      size="sm"
      w={156}
    />
  );
}
