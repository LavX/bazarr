import {
  ActionIcon,
  Anchor,
  Group,
  Popover,
  Stack,
  Text,
  Tooltip,
} from "@mantine/core";
import { faInfoCircle, faRecycle } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { HistoryIcon } from "@/components/bazarr";
import HistoryProvider from "@/components/bazarr/HistoryProvider";
import TextPopover from "@/components/TextPopover";

type HistoryRow = History.EpisodeRow | History.MovieRow;

function historyAnchor(row: Pick<HistoryRow, "origin" | "history_id">) {
  return row.history_id == null
    ? undefined
    : `history-${row.origin ?? "arr"}-${row.history_id}`;
}

export function historyAnchors(rows: HistoryRow[]) {
  return new Map(
    rows
      .filter((row) => row.origin && row.history_id != null)
      .map((row) => [historyAnchor(row), row.source_key ?? null]),
  );
}

export function HistoryAction({ row }: { row: HistoryRow }) {
  return (
    <span id={row.origin ? historyAnchor(row) : undefined}>
      <HistoryIcon action={row.action} />
    </span>
  );
}

function OriginalHistoryDetails({ event }: { event: History.OriginalEvent }) {
  return (
    <Popover width={280} trapFocus>
      <Popover.Target>
        <Anchor component="button" type="button" size="xs">
          Original entry
        </Anchor>
      </Popover.Target>
      <Popover.Dropdown>
        <Stack gap="xs">
          <Group gap="xs">
            <HistoryIcon action={event.action} />
            <HistoryProvider
              action={event.action}
              provider={event.provider ?? undefined}
            />
          </Group>
          {event.timestamp_iso && (
            <Text component="time" dateTime={event.timestamp_iso} size="sm">
              {new Date(event.timestamp_iso).toLocaleString()}
            </Text>
          )}
          {event.description && <Text size="sm">{event.description}</Text>}
        </Stack>
      </Popover.Dropdown>
    </Popover>
  );
}

export function HistoryUpgrade({
  row,
  anchors,
}: {
  row: HistoryRow;
  anchors: ReturnType<typeof historyAnchors>;
}) {
  const original = row.original;
  const verified =
    original &&
    original.origin === row.origin &&
    original.source_key !== "source:removed" &&
    original.source_key === (row.source_key ?? null) &&
    original.history_id !== row.history_id;
  if (!row.upgradable && !verified) return null;

  const target = verified ? historyAnchor(original) : undefined;
  const onPage =
    target !== undefined && anchors.get(target) === original?.source_key;

  return (
    <Group gap="xs" wrap="nowrap">
      {row.upgradable && (
        <TextPopover text="This Subtitle File Is Eligible For An Upgrade.">
          <FontAwesomeIcon size="sm" icon={faRecycle} />
        </TextPopover>
      )}
      {verified &&
        (onPage ? (
          <Anchor href={`#${target}`} size="xs">
            Original entry
          </Anchor>
        ) : (
          <OriginalHistoryDetails event={original} />
        ))}
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
