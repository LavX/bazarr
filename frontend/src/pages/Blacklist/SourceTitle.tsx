import { Stack, Text } from "@mantine/core";

interface Props {
  title: string | null;
  row: Blacklist.SourceRow;
}

export default function SourceTitle({ title, row }: Props) {
  const path = row.source_item_id == null ? row.video_path : null;
  return (
    <Stack gap={2}>
      {title && <Text>{title}</Text>}
      {path && (
        <Text size="xs" c="dimmed" maw="42ch" className="table-long-break">
          {path}
        </Text>
      )}
    </Stack>
  );
}
