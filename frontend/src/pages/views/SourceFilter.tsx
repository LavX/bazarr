import { Select } from "@mantine/core";

interface Props {
  labels: Map<MediaSourceKey, string>;
  value: MediaSourceKey | null;
  onChange: (value: string | null) => void;
  includeSources?: boolean;
}

export default function SourceFilter({
  labels,
  value,
  onChange,
  includeSources = true,
}: Props) {
  if (labels.size <= 1 && !value) return null;
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
