import { Select } from "@mantine/core";

interface Props {
  labels: Map<MediaSourceKey, string>;
  value: MediaSourceKey | null;
  onChange: (value: string | null) => void;
  includeSources?: boolean;
  showSingleSource?: boolean;
  showSingleOwner?: boolean;
  showLabel?: boolean;
}

export default function SourceFilter({
  labels,
  value,
  onChange,
  includeSources = true,
  showSingleSource = false,
  showSingleOwner = false,
  showLabel = false,
}: Props) {
  const hasSource =
    showSingleSource &&
    [...labels.keys()].some((key) => key.startsWith("source:"));
  if (labels.size <= 1 && !value && !hasSource && !showSingleOwner) return null;
  const label = includeSources ? "Source" : "Instance";
  return (
    <Select
      aria-label={label}
      label={showLabel ? label : undefined}
      placeholder={includeSources ? "All sources" : "All instances"}
      data={Array.from(labels, ([value, label]) => ({ value, label }))}
      value={value}
      onChange={onChange}
      allowDeselect
      searchable
      clearable
      clearButtonProps={{
        "aria-label": `Clear ${label.toLowerCase()} filter`,
        "aria-hidden": false,
      }}
      size="sm"
      w={156}
    />
  );
}
