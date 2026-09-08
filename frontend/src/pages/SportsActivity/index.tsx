import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";
import {
  Alert,
  Anchor,
  Button,
  Group,
  Loader,
  Modal,
  Pagination,
  Select,
  Stack,
  Table,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import {
  useRemoveSportsExclusion,
  useSportsAction,
  useSportsActivity,
  useSportsAvailability,
} from "@/apis/hooks/sports";
import type { SportsEvent, SportsRecord } from "@/apis/raw/sports";
import SportsJobFeedback from "./JobFeedback";

const actionNames: Record<number, string> = {
  0: "Deleted",
  1: "Downloaded",
  2: "Manual download",
  3: "Upgraded",
  6: "Translated",
};
export default function SportsActivity({
  kind,
}: {
  kind: "wanted" | "history" | "blacklist";
}) {
  const { enabled, instances, isLoading } = useSportsAvailability();
  const [params, setParams] = useSearchParams();
  const [page, setPage] = useState(1);
  const [language, setLanguage] = useState("");
  const [provider, setProvider] = useState("");
  const [action, setAction] = useState<string | null>(null);
  const [confirmation, setConfirmation] = useState<{
    owner: number;
    record?: SportsRecord;
    clear?: boolean;
  } | null>(null);
  const ownerParam = params.get("instance");
  const owner = ownerParam ? Number(ownerParam) : undefined;
  const eventId = params.get("event_id")
    ? Number(params.get("event_id"))
    : undefined;
  const rows = useSportsActivity(kind, {
    owner,
    eventId,
    language,
    provider,
    action: action ?? undefined,
    page,
  });
  const run = useSportsAction();
  const remove = useRemoveSportsExclusion();
  useEffect(
    () => setPage(1),
    [kind, owner, eventId, language, provider, action],
  );
  if (isLoading) return <Loader />;
  if (!enabled)
    return (
      <Text>Enable a Sportarr instance in Connections to view sports.</Text>
    );
  const selectedOwner =
    owner ?? (instances.length === 1 ? instances[0].id : undefined);
  const title =
    kind === "wanted"
      ? "Missing Sports Subtitles"
      : kind === "history"
        ? "Sports History"
        : "Excluded Sports Subtitles";
  return (
    <Stack p="md">
      <Title order={2}>{title}</Title>
      <Group align="end">
        <Select
          label="Instance"
          placeholder="All instances"
          clearable
          value={owner ? String(owner) : null}
          data={instances.map((instance) => ({
            value: String(instance.id),
            label: instance.name,
          }))}
          onChange={(value) => {
            const next = new URLSearchParams(params);
            next.delete("event_id");
            if (value) next.set("instance", value);
            else next.delete("instance");
            setParams(next);
          }}
        />
        {kind !== "wanted" && (
          <>
            <TextInput
              label="Language"
              placeholder="en or en:hi"
              value={language}
              onChange={(event) => setLanguage(event.currentTarget.value)}
            />
            <TextInput
              label="Provider"
              value={provider}
              onChange={(event) => setProvider(event.currentTarget.value)}
            />
            {kind === "history" && (
              <Select
                label="Action"
                clearable
                value={action}
                onChange={setAction}
                data={Object.entries(actionNames).map(([value, label]) => ({
                  value,
                  label,
                }))}
              />
            )}
          </>
        )}
        {kind === "wanted" && (
          <Button
            disabled={!selectedOwner}
            loading={run.isPending}
            onClick={() =>
              selectedOwner &&
              run.mutate({ path: "/wanted", owner: selectedOwner })
            }
          >
            Search missing
          </Button>
        )}
        {kind === "history" && (
          <Button
            disabled={!selectedOwner}
            loading={run.isPending}
            onClick={() =>
              selectedOwner &&
              run.mutate({ path: "/upgrade", owner: selectedOwner })
            }
          >
            Search upgrades
          </Button>
        )}
        {kind === "blacklist" && (
          <Button
            color="red"
            variant="light"
            disabled={!selectedOwner || !rows.data?.total}
            onClick={() =>
              selectedOwner &&
              setConfirmation({ owner: selectedOwner, clear: true })
            }
          >
            Clear instance exclusions
          </Button>
        )}
      </Group>
      {!selectedOwner && (
        <Text size="sm" c="dimmed">
          Select an instance to run an action for its library.
        </Text>
      )}
      {eventId && (
        <Group>
          <Text size="sm">Showing this event's history and exclusions.</Text>
          <Button
            variant="subtle"
            size="xs"
            onClick={() => {
              const next = new URLSearchParams(params);
              next.delete("event_id");
              setParams(next);
            }}
          >
            Show all events
          </Button>
        </Group>
      )}
      {(rows.isError || run.isError || remove.isError) && (
        <Alert color="red">
          Could not complete the sports request. Check the selected instance and
          try again.
        </Alert>
      )}
      <SportsJobFeedback queued={run.data} owner={run.variables?.owner} />
      {remove.isSuccess && (
        <Text>
          Exclusion removed. The release is eligible for future searches.
        </Text>
      )}
      {rows.isLoading && <Loader />}
      {rows.data?.total === 0 && (
        <Text>
          No matching sports{" "}
          {kind === "wanted" ? "files with missing subtitles" : "records"}.
        </Text>
      )}
      {rows.data && rows.data.total > 0 && (
        <Table.ScrollContainer minWidth={800}>
          <Table striped highlightOnHover aria-label={title}>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Event</Table.Th>
                <Table.Th>Instance</Table.Th>
                <Table.Th>Language</Table.Th>
                {kind !== "wanted" && (
                  <>
                    <Table.Th>Provider</Table.Th>
                    <Table.Th>Time</Table.Th>
                  </>
                )}
                {kind === "history" && (
                  <>
                    <Table.Th>Action</Table.Th>
                    <Table.Th>Score</Table.Th>
                  </>
                )}
                <Table.Th>Actions</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {rows.data.data.map((item) => {
                const event =
                  kind === "wanted" ? (item as SportsEvent) : undefined;
                const record =
                  kind !== "wanted" ? (item as SportsRecord) : undefined;
                return (
                  <Table.Tr key={item.id}>
                    <Table.Td>
                      <Anchor
                        component={Link}
                        to={`/sports/${item.league_id}?instance=${item.arr_instance_id}`}
                      >
                        {item.title}
                      </Anchor>
                      {record?.description && (
                        <Text size="xs" c="dimmed">
                          {record.description}
                        </Text>
                      )}
                    </Table.Td>
                    <Table.Td>
                      {
                        instances.find(
                          (instance) => instance.id === item.arr_instance_id,
                        )?.name
                      }
                    </Table.Td>
                    <Table.Td>
                      {event?.missing_subtitles.join(", ") ?? record?.language}
                    </Table.Td>
                    {record && (
                      <>
                        <Table.Td>{record.provider}</Table.Td>
                        <Table.Td>
                          {record.timestamp
                            ? new Date(record.timestamp).toLocaleString()
                            : "Unknown"}
                        </Table.Td>
                      </>
                    )}
                    {kind === "history" && (
                      <>
                        <Table.Td>
                          {actionNames[record?.action ?? -1] ?? "Other"}
                        </Table.Td>
                        <Table.Td>
                          {record?.score != null && record.score_out_of
                            ? `${Math.round((record.score * 100) / record.score_out_of)}%`
                            : "Unknown"}
                        </Table.Td>
                      </>
                    )}
                    <Table.Td>
                      {event && (
                        <Button
                          size="xs"
                          loading={run.isPending}
                          onClick={() =>
                            run.mutate({
                              path: `/events/${event.id}/automatic`,
                              owner: event.arr_instance_id,
                            })
                          }
                        >
                          Search missing
                        </Button>
                      )}
                      {record &&
                        kind === "history" &&
                        [1, 2, 3].includes(record.action ?? -1) &&
                        record.provider &&
                        record.subs_id && (
                          <Button
                            size="xs"
                            variant="light"
                            color="red"
                            onClick={() =>
                              setConfirmation({
                                owner: record.arr_instance_id,
                                record,
                              })
                            }
                          >
                            Exclude release
                          </Button>
                        )}
                      {record && kind === "blacklist" && (
                        <Button
                          size="xs"
                          variant="light"
                          loading={remove.isPending}
                          onClick={() =>
                            remove.mutate({
                              owner: record.arr_instance_id,
                              id: record.id,
                            })
                          }
                        >
                          Remove exclusion
                        </Button>
                      )}
                    </Table.Td>
                  </Table.Tr>
                );
              })}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      )}
      {(rows.data?.total ?? 0) > 100 && (
        <Pagination
          value={page}
          onChange={setPage}
          total={Math.ceil((rows.data?.total ?? 0) / 100)}
        />
      )}
      <Modal
        opened={confirmation !== null}
        onClose={() => setConfirmation(null)}
        title={
          confirmation?.clear
            ? "Clear sports exclusions"
            : "Exclude sports release"
        }
        centered
      >
        <Stack>
          <Text>
            {confirmation?.clear
              ? "Allow every excluded release for this instance again?"
              : "Exclude this provider release for this instance and search for a replacement? The saved subtitle is deleted only when it still matches this download. An unproven or newer file is preserved."}
          </Text>
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setConfirmation(null)}>
              Cancel
            </Button>
            <Button
              color="red"
              loading={run.isPending || remove.isPending}
              onClick={() => {
                if (!confirmation) return;
                if (confirmation.clear)
                  remove.mutate(
                    { owner: confirmation.owner },
                    { onSuccess: () => setConfirmation(null) },
                  );
                else if (confirmation.record)
                  run.mutate(
                    {
                      path: `/history/${confirmation.record.id}/blacklist`,
                      owner: confirmation.owner,
                    },
                    { onSuccess: () => setConfirmation(null) },
                  );
              }}
            >
              {confirmation?.clear ? "Clear exclusions" : "Exclude release"}
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
