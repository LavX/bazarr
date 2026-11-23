import { Select } from "@mantine/core";

interface Props {
  labels: Map<MediaSourceKey, string>;
  value: MediaSourceKey | null;
  onChange: (value: string | null) => void;
  includeSources?: boolean;
  showSingleSource?: boolean;
}

export default function SourceFilter({
  labels,
  value,
  onChange,
  includeSources = true,
  showSingleSource = false,
}: Props) {
  const hasSource =
    showSingleSource &&
    [...labels.keys()].some((key) => key.startsWith("source:"));
  if (labels.size <= 1 && !value && !hasSource) return null;
  return (
    <Select
      aria-label={includeSources ? "Source" : "Instance"}
      placeholder={includeSources ? "All sources" : "All instances"}
      data={Array.from(labels, ([value, label]) => ({ value, label }))}
      value={value}
      onChange={onChange}
      allowDeselect
      searchable
      clearable
      size="sm"
      w={156}
    />
  );
}
