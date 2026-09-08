import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import {
  Alert,
  Anchor,
  Badge,
  Button,
  Group,
  Image,
  Loader,
  Pagination,
  Stack,
  Table,
  Text,
  Title,
} from "@mantine/core";
import {
  useIndexSportsSubtitles,
  useSportsAction,
  useSportsAvailability,
  useSportsEvents,
  useSportsLeague,
} from "@/apis/hooks/sports";
import { SportsSearchModal } from "@/components/modals/SportsSearchModal";
import { useModals } from "@/modules/modals";
import SportsJobFeedback from "@/pages/SportsActivity/JobFeedback";

export default function SportsEvents() {
  const { id } = useParams();
  const modals = useModals();
  const [params] = useSearchParams();
  const { enabled, isLoading } = useSportsAvailability();
  const owner = params.get("instance");
  const league = useSportsLeague(Number(id), owner ? Number(owner) : undefined);
  const [page, setPage] = useState(1);
  const indexSubtitles = useIndexSportsSubtitles();
  const automatic = useSportsAction();
  const events = useSportsEvents(
    Number(id),
    league.data?.arr_instance_id,
    page,
  );
  useEffect(() => setPage(1), [id, owner]);
  if (isLoading || league.isLoading) return <Loader />;
  if (!enabled)
    return (
      <Text>Enable a Sportarr instance in Connections to view sports.</Text>
    );
  if (league.isError || !league.data)
    return <Alert color="red">League not found.</Alert>;
  return (
    <Stack p="md">
      <Anchor component={Link} to="/sports">
        Sports
      </Anchor>
      <Group justify="space-between">
        <Title order={2}>{league.data.title}</Title>
        <Button
          loading={automatic.isPending}
          disabled={league.data.profileId == null}
          onClick={() =>
            league.data &&
            automatic.mutate({
              path: `/leagues/${league.data.id}/download`,
              owner: league.data.arr_instance_id,
            })
          }
        >
          Download all missing
        </Button>
      </Group>
      {automatic.isError && (
        <Alert color="red">Could not queue the sports search.</Alert>
      )}
      <SportsJobFeedback
        queued={automatic.data}
        owner={automatic.variables?.owner}
      />
      {league.data.fanart && (
        <Image src={league.data.fanart} alt="" mah={240} fit="contain" />
      )}
      <Text>{league.data.overview}</Text>
      <Text>
        {league.data.eventCount} events · {league.data.eventFileCount} files
      </Text>
      {events.isLoading && <Loader aria-label="Loading event files" />}
      {events.isError && <Alert color="red">Could not load event files.</Alert>}
      {indexSubtitles.isError && (
        <Alert color="red">
          Could not index subtitles. Check the file path and try again.
        </Alert>
      )}
      {events.data &&
        (events.data.total === 0 ? (
          <Text>No playable event files in this league.</Text>
        ) : (
          <>
            <Table.ScrollContainer minWidth={600}>
              <Table aria-label="Event files" striped highlightOnHover>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Event</Table.Th>
                    <Table.Th>Date</Table.Th>
                    <Table.Th>Part</Table.Th>
                    <Table.Th>File</Table.Th>
                    <Table.Th>Subtitles</Table.Th>
                    <Table.Th>Missing</Table.Th>
                    <Table.Th>Actions</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {events.data.data.map((event) => (
                    <Table.Tr key={event.id}>
                      <Table.Td>{event.title}</Table.Td>
                      <Table.Td>
                        {(event.broadcastDate || event.eventDate)?.slice(
                          0,
                          10,
                        ) || "Unknown date"}
                      </Table.Td>
                      <Table.Td>
                        {event.partName ||
                          (event.partNumber && event.partNumber > 0
                            ? `Part ${event.partNumber}`
                            : "Full event")}
                      </Table.Td>
                      <Table.Td>
                        <Text size="sm">
                          {event.hasFile ? "Available" : "Unavailable"}
                        </Text>
                        <Text size="xs" c="dimmed" title={event.path}>
                          {event.path.split(/[\\/]/).pop()}
                        </Text>
                      </Table.Td>
                      <Table.Td>
                        <Group gap="xs">
                          {event.subtitles?.length ? (
                            event.subtitles.map(([language, path], index) => (
                              <Badge
                                key={`${language}-${path}-${index}`}
                                variant="light"
                                title={path || undefined}
                              >
                                {language} · {path ? "External" : "Embedded"}
                              </Badge>
                            ))
                          ) : (
                            <Text size="sm" c="dimmed">
                              No indexed subtitles
                            </Text>
                          )}
                        </Group>
                      </Table.Td>
                      <Table.Td>
                        {event.profileId == null ? (
                          <Text size="sm" c="dimmed">
                            No language profile
                          </Text>
                        ) : (
                          <Group gap="xs">
                            {event.missing_subtitles?.length ? (
                              event.missing_subtitles.map((language) => (
                                <Badge
                                  key={language}
                                  color="orange"
                                  variant="light"
                                >
                                  {language}
                                </Badge>
                              ))
                            ) : (
                              <Text size="sm">None missing</Text>
                            )}
                          </Group>
                        )}
                      </Table.Td>
                      <Table.Td>
                        <Button
                          size="xs"
                          variant="light"
                          disabled={
                            event.profileId == null || automatic.isPending
                          }
                          onClick={() =>
                            automatic.mutate({
                              path: `/events/${event.id}/automatic`,
                              owner: event.arr_instance_id,
                            })
                          }
                        >
                          Download missing
                        </Button>
                        <Anchor
                          component={Link}
                          size="xs"
                          display="block"
                          to={`/history/sports?instance=${event.arr_instance_id}&event_id=${event.id}`}
                        >
                          History and release exclusions
                        </Anchor>
                        <Button
                          size="xs"
                          variant="light"
                          disabled={indexSubtitles.isPending}
                          loading={
                            indexSubtitles.isPending &&
                            indexSubtitles.variables?.id === event.id
                          }
                          onClick={() =>
                            indexSubtitles.mutate({
                              id: event.id,
                              owner: event.arr_instance_id,
                            })
                          }
                        >
                          Index subtitles
                        </Button>
                        <Button
                          size="xs"
                          variant="subtle"
                          disabled={!event.hasFile}
                          onClick={() =>
                            modals.openContextModal(SportsSearchModal, {
                              item: event,
                            })
                          }
                        >
                          Search subtitles
                        </Button>
                      </Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            </Table.ScrollContainer>
            {events.data.total > 100 && (
              <Pagination
                total={Math.ceil(events.data.total / 100)}
                value={page}
                onChange={setPage}
              />
            )}
          </>
        ))}
    </Stack>
  );
}
