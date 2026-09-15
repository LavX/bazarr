import { FunctionComponent, ReactNode } from "react";
import { Card, Group, Text } from "@mantine/core";

interface Props {
  label: string;
  value: ReactNode;
  /** Secondary line under the value: a denominator, a window, or a caveat. */
  hint?: ReactNode;
  /** Mantine colour for the value, used to mark a degraded reading. */
  color?: string;
  icon?: ReactNode;
}

const StatTile: FunctionComponent<Props> = ({
  label,
  value,
  hint,
  color,
  icon,
}) => (
  <Card withBorder padding="md" radius="md">
    <Group justify="space-between" align="flex-start" wrap="nowrap" gap="xs">
      <Text size="xs" c="dimmed" tt="uppercase" fw={600}>
        {label}
      </Text>
      {icon}
    </Group>
    <Text size="xl" fw={700} c={color}>
      {value}
    </Text>
    {hint ? (
      <Text size="xs" c="dimmed">
        {hint}
      </Text>
    ) : null}
  </Card>
);

export default StatTile;
