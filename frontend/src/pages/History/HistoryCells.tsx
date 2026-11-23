import { ActionIcon, Anchor, Group, Stack, Text, Tooltip } from "@mantine/core";
import { faInfoCircle, faRecycle } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { HistoryIcon } from "@/components/bazarr";
import TextPopover from "@/components/TextPopover";

type HistoryRow = History.EpisodeRow | History.MovieRow;

function historyAnchor(row: HistoryRow, id = row.history_id) {
  return id == null ? undefined : `history-${row.origin ?? "arr"}-${id}`;
}

export function historyAnchors(rows: HistoryRow[]) {
  return new Set(rows.map((row) => historyAnchor(row)));
}

export function HistoryAction({ row }: { row: HistoryRow }) {
  return (
    <span id={row.origin ? historyAnchor(row) : undefined}>
      <HistoryIcon action={row.action} />
    </span>
  );
}

export function HistoryUpgrade({
  row,
  anchors,
}: {
  row: HistoryRow;
  anchors: Set<string | undefined>;
}) {
  const originalId = row.upgradedFromId ?? row.original_id;
  const target =
    originalId != null && originalId !== row.history_id
      ? historyAnchor(row, originalId)
      : undefined;
  const link = row.origin && target && anchors.has(target);
  if (!row.upgradable && !link) return null;

  return (
    <Group gap="xs" wrap="nowrap">
      {row.upgradable && (
        <TextPopover text="This Subtitle File Is Eligible For An Upgrade.">
          <FontAwesomeIcon size="sm" icon={faRecycle} />
        </TextPopover>
      )}
      {link && (
        <Anchor href={`#${target}`} size="xs">
          Original entry
        </Anchor>
      )}
    </Group>
  );
}

export function HistoryInfo({ row }: { row: HistoryRow }) {
  const icon = <FontAwesomeIcon size="sm" icon={faInfoCircle} />;

  if (row.origin !== "source" || !row.video_path) {
    return <TextPopover text={row.description}>{icon}</TextPopover>;
  }

  return (
    <Tooltip
      multiline
      maw={360}
      label={
        <Stack gap={4} style={{ overflowWrap: "anywhere" }}>
          {row.description && <Text size="sm">{row.description}</Text>}
          <Text size="sm">{row.video_path}</Text>
        </Stack>
      }
      events={{ hover: true, focus: true, touch: true }}
    >
      <ActionIcon
        variant="transparent"
        color="gray"
        size="xs"
        aria-label="History details"
      >
        {icon}
      </ActionIcon>
    </Tooltip>
  );
}
