import { useState } from "react";
import { Link } from "react-router";
import {
  Alert,
  Anchor,
  Badge,
  Button,
  Card,
  Group,
  Image,
  Loader,
  NativeSelect,
  Pagination,
  Select,
  SimpleGrid,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { useLanguageProfiles } from "@/apis/hooks";
import {
  useSportsAvailability,
  useSportsLeagues,
  useSportsProfile,
  useSyncSports,
} from "@/apis/hooks/sports";
import { LIBRARY_ROUTES } from "@/Router/mediaRoutes";

export default function Sports() {
  const { instances, enabled, isLoading } = useSportsAvailability();
  const [owner, setOwner] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const selectedOwner = instances.some(
    (instance) => instance.id === Number(owner),
  )
    ? owner
    : null;
  const leagues = useSportsLeagues(
    selectedOwner ? Number(selectedOwner) : undefined,
    page,
  );
  const { data: profiles } = useLanguageProfiles();
  const assign = useSportsProfile();
  const sync = useSyncSports();
  if (isLoading) return <Loader />;
  if (!enabled)
    return (
      <Text>Enable a Sportarr instance in Connections to view sports.</Text>
    );
  const names = new Map(
    instances.map((instance) => [instance.id, instance.name]),
  );
  return (
    <Stack p="md">
      <Group justify="space-between">
        <Title order={2}>Sports</Title>
        <Button
          loading={sync.isPending}
          onClick={() =>
            sync.mutate(
              selectedOwner
                ? [Number(selectedOwner)]
                : instances.map((instance) => instance.id),
            )
          }
        >
          Sync leagues
        </Button>
      </Group>
      {instances.length > 1 && (
        <Select
          label="Instance"
          placeholder="All instances"
          clearable
          value={selectedOwner}
          onChange={(value) => {
            setOwner(value);
            setPage(1);
          }}
          data={instances.map((instance) => ({
            value: String(instance.id),
            label: instance.name,
          }))}
        />
      )}
      {(leagues.isError || assign.isError || sync.isError) && (
        <Alert color="red">
          Could not update the sports library. Please try again.
        </Alert>
      )}
      {sync.isSuccess && <Text size="sm">League sync queued.</Text>}
      {leagues.isLoading && <Loader />}
      {leagues.data?.total === 0 && (
        <Text c="dimmed">
          No leagues synced yet. Sync your Sportarr library to get started.
        </Text>
      )}
      <SimpleGrid cols={{ base: 1, sm: 2, md: 3, xl: 4 }}>
        {leagues.data?.data.map((league) => (
          <Card
            component="section"
            aria-label={`${league.title} league`}
            key={league.id}
            withBorder
          >
            <Image
              src={league.poster}
              alt={`${league.title} poster`}
              h={220}
              fit="contain"
              mb="sm"
            />
            <Anchor
              component={Link}
              to={`${LIBRARY_ROUTES.sportarr}/${league.id}?instance=${league.arr_instance_id}`}
              fw={600}
            >
              {league.title}
            </Anchor>
            <Group gap="xs" my="xs">
              <Badge variant="light">{league.sport || "Sport"}</Badge>
              <Text size="xs" c="dimmed">
                {names.get(league.arr_instance_id)}
              </Text>
            </Group>
            <Text size="sm" mb="sm">
              {league.eventCount} events · {league.eventFileCount} files
            </Text>
            <NativeSelect
              label={`Profile for ${league.title}`}
              data={[
                { value: "none", label: "No profile" },
                ...(profiles ?? []).map((profile) => ({
                  value: String(profile.profileId),
                  label: profile.name,
                })),
              ]}
              value={
                league.profileId === null ? "none" : String(league.profileId)
              }
              disabled={assign.isPending}
              onChange={(event) => {
                const value = event.currentTarget.value;
                assign.mutate({
                  id: league.id,
                  owner: league.arr_instance_id,
                  profileId: value === "none" ? null : Number(value),
                });
              }}
            />
          </Card>
        ))}
      </SimpleGrid>
      {(leagues.data?.total ?? 0) > 100 && (
        <Pagination
          value={page}
          onChange={setPage}
          total={Math.ceil((leagues.data?.total ?? 0) / 100)}
        />
      )}
    </Stack>
  );
}
