import { FunctionComponent } from "react";
import { Badge, Group } from "@mantine/core";
import { getCombinedLabel } from "@/utilities/subtitles";

interface Props {
  subtitle: Subtitle;
}

// Mirrors how an ordinary subtitle renders in the same row: the languages are
// the primary badge, and the modifier rides along in a small secondary one, the
// way a sync-engine output does. Rendering the languages as bare text put
// unstyled words in the middle of a line of pills, which reads as a rendering
// fault rather than as a deliberate difference.
const CombinedSubtitleBadge: FunctionComponent<Props> = ({ subtitle }) => {
  const isAss = subtitle.path?.toLowerCase().endsWith(".ass") ?? false;
  const label = getCombinedLabel(subtitle);

  return (
    <Group gap={4} wrap="nowrap">
      <Badge style={{ whiteSpace: "nowrap" }}>{label}</Badge>
      <Badge
        color={isAss ? "violet" : "gray"}
        size="xs"
        variant="light"
        style={{ whiteSpace: "nowrap" }}
      >
        Combined ({isAss ? "ASS" : "SRT"})
      </Badge>
    </Group>
  );
};

export default CombinedSubtitleBadge;
