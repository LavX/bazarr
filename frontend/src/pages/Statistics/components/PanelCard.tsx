import { FunctionComponent, ReactNode } from "react";
import { Card, Text, Title } from "@mantine/core";

interface Props {
  title: string;
  /**
   * Honest small print about what the figures below do and do not cover, for
   * example a capped source or a window shorter than it looks. Rendered under
   * the title so it cannot be missed.
   */
  caveat?: ReactNode;
  children: ReactNode;
}

const PanelCard: FunctionComponent<Props> = ({ title, caveat, children }) => (
  <Card withBorder padding="md" radius="md">
    <Title order={5}>{title}</Title>
    {caveat ? (
      <Text size="xs" c="dimmed" mt={2} mb="sm">
        {caveat}
      </Text>
    ) : (
      <div style={{ height: "var(--mantine-spacing-sm)" }} />
    )}
    {children}
  </Card>
);

export default PanelCard;
