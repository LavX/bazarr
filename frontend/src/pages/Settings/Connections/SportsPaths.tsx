import { Button, Group, Stack, Text, TextInput } from "@mantine/core";
import type { ArrPathMapping } from "@/apis/raw/arrInstances";

interface Props {
  value: ArrPathMapping[];
  onChange: (value: ArrPathMapping[]) => void;
}

export default function SportsPaths({ value, onChange }: Props) {
  const update = (index: number, side: 0 | 1, path: string) => {
    onChange(
      value.map((pair, current) =>
        current === index
          ? side === 0
            ? [path, pair[1]]
            : [pair[0], path]
          : pair,
      ),
    );
  };
  return (
    <Stack gap="xs">
      <Text size="sm">
        Map paths when Sportarr and Bazarr see the same files in different
        locations. These mappings apply only to this instance.
      </Text>
      {value.map((pair, index) => (
        <Group key={index} align="end" wrap="wrap">
          <TextInput
            label={`Sportarr path ${index + 1}`}
            value={pair[0]}
            onChange={(event) => update(index, 0, event.currentTarget.value)}
            placeholder="/sports"
            style={{ flex: 1 }}
          />
          <TextInput
            label={`Bazarr path ${index + 1}`}
            value={pair[1]}
            onChange={(event) => update(index, 1, event.currentTarget.value)}
            placeholder="/media/sports"
            style={{ flex: 1 }}
          />
          <Button
            variant="subtle"
            color="red"
            aria-label={`Remove mapping ${index + 1}`}
            onClick={() =>
              onChange(value.filter((_, current) => current !== index))
            }
          >
            Remove
          </Button>
        </Group>
      ))}
      <Button variant="light" onClick={() => onChange([...value, ["", ""]])}>
        Add path mapping
      </Button>
    </Stack>
  );
}
