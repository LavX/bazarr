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
  Stack,
  Text,
} from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import { faTrash } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { ColumnDef } from "@tanstack/react-table";
import { useArrInstanceLabels } from "@/apis/hooks/arrInstances";
import { useInstanceName } from "@/apis/hooks/site";
import {
  SportsActivityRow,
  useRemoveSportsExclusion,
  useSportsAvailability,
  useSportsBlacklistPagination,
} from "@/apis/hooks/sports";
import { QueryPageTable } from "@/components";
import { InstanceBadge } from "@/components/bazarr";
import Language from "@/components/bazarr/Language";
import TextPopover from "@/components/TextPopover";
import SportsActivityFilters from "@/pages/views/SportsActivityFilters";

// Built as its own page on the shared paginated table, like the Series and
// Movies exclusion pages. It used to be one branch of a component shared with
// Wanted and History, so it inherited their columns and none of the paging or
// styling the other two exclusion pages have.
const BlacklistSportsView: FunctionComponent = () => {
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
  const [confirmClear, setConfirmClear] = useState<number | null>(null);

  const query = useSportsBlacklistPagination({
    owner,
    eventId,
    language,
    provider,
  });
  const remove = useRemoveSportsExclusion();
  const {
    multiInstance,
    nameById: instanceNameById,
    defaultId: instanceDefaultId,
  } = useArrInstanceLabels("sportarr");

  const columns = useMemo<ColumnDef<SportsActivityRow>[]>(
    () => [
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
        id: "remove",
        cell: ({ row: { original } }) => (
          <ActionIcon
            aria-label="Remove exclusion"
            variant="subtle"
            loading={remove.isPending}
            onClick={() =>
              remove.mutate({
                owner: original.arr_instance_id,
                id: original.id,
              })
            }
          >
            <FontAwesomeIcon size="sm" icon={faTrash} />
          </ActionIcon>
        ),
      },
    ],
    [multiInstance, instanceNameById, instanceDefaultId, remove],
  );

  useDocumentTitle(`Sports Excluded - ${useInstanceName()}`);

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
    <Container fluid px={0}>
      <Stack gap="xs">
        <SportsActivityFilters
          language={language}
          onLanguageChange={setLanguage}
          provider={provider}
          onProviderChange={setProvider}
          actionLabel="Remove All"
          actionColor="red"
          actionDisabled={query.paginationStatus.totalCount === 0}
          actionLoading={remove.isPending}
          onAction={setConfirmClear}
        />
        {remove.isSuccess && (
          <Text size="sm">
            Exclusion removed. The release is eligible for future searches.
          </Text>
        )}
        <QueryPageTable
          tableStyles={{ emptyText: "Nothing Excluded for Sports" }}
          columns={columns}
          query={query}
        ></QueryPageTable>
      </Stack>
      <Modal
        opened={confirmClear !== null}
        onClose={() => setConfirmClear(null)}
        title="Clear sports exclusions"
        centered
      >
        <Stack>
          <Text>
            Allow every excluded release for this instance again? Removing an
            exclusion does not bring the subtitle back; it makes that release
            eligible for a future search.
          </Text>
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setConfirmClear(null)}>
              Cancel
            </Button>
            <Button
              color="red"
              loading={remove.isPending}
              onClick={() =>
                confirmClear !== null &&
                remove.mutate(
                  { owner: confirmClear },
                  { onSuccess: () => setConfirmClear(null) },
                )
              }
            >
              Clear exclusions
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Container>
  );
};

export default BlacklistSportsView;
