import { Select } from "@mantine/core";

interface Props {
  labels: Map<MediaSourceKey, string>;
  value: MediaSourceKey | null;
  onChange: (value: string | null) => void;
}

export default function SourceFilter({ labels, value, onChange }: Props) {
  if (labels.size <= 1 && !value) return null;
  return (
    <Select
      aria-label="Source"
      placeholder="All sources"
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
