import { FunctionComponent, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router";
import {
  ActionIcon,
  Anchor,
  Badge,
  Button,
  Container,
  Group,
  Modal,
  Select,
  Stack,
  Switch,
  Text,
} from "@mantine/core";
import {
  faFileExcel,
  faInfoCircle,
  faRecycle,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { ColumnDef } from "@tanstack/react-table";
import { useArrInstanceLabels } from "@/apis/hooks/arrInstances";
import {
  SportsActivityRow,
  useSportsAction,
  useSportsAvailability,
  useSportsHistoryPagination,
} from "@/apis/hooks/sports";
import {
  HistoryIcon,
  InstanceBadge,
  SportsJobFeedback,
} from "@/components/bazarr";
import Language from "@/components/bazarr/Language";
import StateIcon from "@/components/StateIcon";
import TextPopover from "@/components/TextPopover";
import HistoryView from "@/pages/views/HistoryView";
import SportsActivityFilters from "@/pages/views/SportsActivityFilters";

// The actions a sports history row can carry. Kept here rather than shared with
// HistoryIcon: that component covers every action Bazarr records, while this
// list is what the Action filter offers, which is only what sports produce.
export const SPORTS_HISTORY_ACTIONS: [string, string][] = [
  ["0", "Deleted"],
  ["1", "Downloaded"],
  ["2", "Manual download"],
  ["3", "Upgraded"],
  ["4", "Uploaded"],
  ["5", "Synced"],
  ["6", "Translated"],
];

// An exclusion deletes a file, so it asks first. Same wording the old combined
// page used, which was the one thing worth keeping from it.
const EXCLUDE_PROMPT =
  "Exclude this provider release for this instance and search for a replacement? " +
  "The saved subtitle is deleted only when it still matches this download. " +
  "An unproven or newer file is preserved.";

const SportsHistoryView: FunctionComponent = () => {
  const { enabled, isLoading } = useSportsAvailability();
  const [params] = useSearchParams();
  const owner = params.get("instance")
    ? Number(params.get("instance"))
    : undefined;
  const eventId = params.get("event_id")
    ? Number(params.get("event_id"))
    : undefined;

  const [language, setLanguage] = useState("");
  const [provider, setProvider] = useState("");
  const [action, setAction] = useState<string | null>(null);
  const [pending, setPending] = useState<SportsActivityRow | null>(null);
  // Embedded Source rows (track state, not events) are hidden by default; the
  // switch asks the API to include them, exactly like the other two pages.
  const [includeEmbedded, setIncludeEmbedded] = useState(false);

  const query = useSportsHistoryPagination({
    owner,
    eventId,
    language,
    provider,
    action: action ?? undefined,
    includeEmbedded,
  });
  const run = useSportsAction();
  const {
    multiInstance,
    nameById: instanceNameById,
    defaultId: instanceDefaultId,
  } = useArrInstanceLabels("sportarr");

  const columns = useMemo<ColumnDef<SportsActivityRow>[]>(
    () => [
      {
        id: "action",
        cell: ({ row }) => <HistoryIcon action={row.original.action ?? -1} />,
      },
      {
        header: "Name",
        accessorKey: "title",
        cell: ({ row: { original } }) => (
          <Anchor
            className="table-primary"
            component={Link}
            to={`/sports/${original.league_id}?instance=${original.arr_instance_id}`}
          >
            {original.title}
          </Anchor>
        ),
      },
      ...(multiInstance
        ? [
            {
              id: "instance",
              header: "Instance",
              cell: ({ row: { original } }) => (
                <InstanceBadge
                  instanceId={original.arr_instance_id}
                  defaultId={instanceDefaultId}
                  nameById={instanceNameById}
                />
              ),
            } as ColumnDef<SportsActivityRow>,
          ]
        : []),
      {
        header: "Language",
        accessorKey: "language",
        cell: ({ row: { original } }) =>
          original.language ? (
            <Badge>
              <Language.Text value={original.language} long></Language.Text>
            </Badge>
          ) : null,
      },
      {
        header: "Provider",
        accessorKey: "provider",
      },
      {
        header: "Score",
        accessorKey: "score",
      },
      {
        header: "Match",
        accessorKey: "matches",
        cell: ({ row: { original } }) => {
          const { matches, dont_matches: dont } = original;
          if (matches.length || dont.length) {
            return <StateIcon matches={matches} dont={dont} isHistory={true} />;
          }
          return null;
        },
      },
      {
        header: "Date",
        accessorKey: "timestamp",
        cell: ({ row: { original } }) =>
          original.timestamp ? (
            <TextPopover text={original.parsed_timestamp}>
              <Text>{original.timestamp}</Text>
            </TextPopover>
          ) : null,
      },
      {
        header: "Info",
        accessorKey: "description",
        cell: ({ row: { original } }) =>
          original.description ? (
            <TextPopover text={original.description}>
              <FontAwesomeIcon size="sm" icon={faInfoCircle} />
            </TextPopover>
          ) : null,
      },
      {
        header: "Upgradable",
        accessorKey: "upgradable",
        cell: ({ row: { original } }) =>
          original.upgradable ? (
            <TextPopover text="This Subtitle File Is Eligible For An Upgrade.">
              <FontAwesomeIcon size="sm" icon={faRecycle} />
            </TextPopover>
          ) : null,
      },
      {
        header: "Excluded",
        id: "exclude",
        cell: ({ row: { original } }) => {
          // Only a provider download can be excluded: an exclusion names the
          // release, and a deletion, upload or sync has none to name.
          if (
            ![1, 2, 3].includes(original.action ?? -1) ||
            !original.provider ||
            !original.subs_id
          ) {
            return null;
          }
          // A row that already names an exclusion cannot be excluded again:
          // the repeat queues a second job and a replacement search for a
          // release the instance has been told to skip.
          return (
            <ActionIcon
              aria-label="Exclude"
              variant="subtle"
              color="red"
              disabled={original.blacklisted}
              onClick={() => setPending(original)}
            >
              <FontAwesomeIcon size="sm" icon={faFileExcel} />
            </ActionIcon>
          );
        },
      },
    ],
    [multiInstance, instanceNameById, instanceDefaultId],
  );

  if (isLoading) return null;
  if (!enabled) {
    return (
      <Container px={0} fluid>
        <Text p="md">
          Enable a Sportarr instance in Connections to view sports.
        </Text>
      </Container>
    );
  }

  return (
    <>
      <HistoryView
        name="Sports"
        query={query}
        columns={columns}
        toolbar={
          <Stack gap="xs" mb="xs">
            <SportsActivityFilters
              language={language}
              onLanguageChange={setLanguage}
              provider={provider}
              onProviderChange={setProvider}
              extra={
                <Select
                  label="Action"
                  clearable
                  value={action}
                  onChange={setAction}
                  data={SPORTS_HISTORY_ACTIONS.map(([value, label]) => ({
                    value,
                    label,
                  }))}
                />
              }
              actionLabel="Search upgrades"
              actionLoading={run.isPending}
              onAction={(selected) =>
                run.mutate({ path: "/upgrade", owner: selected })
              }
            />
            <SportsJobFeedback queued={run.data} owner={run.variables?.owner} />
            <Group justify="flex-end">
              <Switch
                label="Show Embedded Source records"
                checked={includeEmbedded}
                onChange={(event) =>
                  setIncludeEmbedded(event.currentTarget.checked)
                }
              ></Switch>
            </Group>
          </Stack>
        }
      ></HistoryView>
      <Modal
        opened={pending !== null}
        onClose={() => setPending(null)}
        title="Exclude sports release"
        centered
      >
        <Stack>
          <Text>{EXCLUDE_PROMPT}</Text>
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setPending(null)}>
              Cancel
            </Button>
            <Button
              color="red"
              loading={run.isPending}
              onClick={() =>
                pending &&
                run.mutate(
                  {
                    path: `/history/${pending.id}/blacklist`,
                    owner: pending.arr_instance_id,
                  },
                  { onSuccess: () => setPending(null) },
                )
              }
            >
              Exclude release
            </Button>
          </Group>
        </Stack>
      </Modal>
    </>
  );
};

export default SportsHistoryView;
