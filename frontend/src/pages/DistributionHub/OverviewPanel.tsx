import { FunctionComponent } from "react";
import {
  Card,
  Group,
  SimpleGrid,
  Stack,
  Table,
  Text,
  Title,
} from "@mantine/core";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useDistStatsOverview, useDistStatsTimeseries } from "@/apis/hooks";
import { QueryOverlay } from "@/components/async";

const StatCard: FunctionComponent<{
  label: string;
  value: number | string;
  hint?: string;
}> = ({ label, value, hint }) => (
  <Card withBorder padding="md" radius="md">
    <Text size="xs" c="dimmed" tt="uppercase" fw={600}>
      {label}
    </Text>
    <Text size="xl" fw={700}>
      {value}
    </Text>
    {hint && (
      <Text size="xs" c="dimmed">
        {hint}
      </Text>
    )}
  </Card>
);

const OverviewPanel: FunctionComponent = () => {
  const overview = useDistStatsOverview();
  const series = useDistStatsTimeseries({ range_days: 30 });

  const data = overview.data;

  return (
    <QueryOverlay result={overview}>
      <Stack gap="lg">
        <SimpleGrid cols={{ base: 1, sm: 2, lg: 4 }}>
          <StatCard
            label="Searches today"
            value={data?.totals.today.search ?? 0}
            hint={`${data?.totals.d7.search ?? 0} in 7d / ${
              data?.totals.d30.search ?? 0
            } in 30d`}
          />
          <StatCard
            label="Downloads today"
            value={data?.totals.today.download ?? 0}
            hint={`${data?.totals.d7.download ?? 0} in 7d / ${
              data?.totals.d30.download ?? 0
            } in 30d`}
          />
          <StatCard
            label="Active keys"
            value={`${data?.active_count ?? 0} / ${data?.key_count ?? 0}`}
            hint={`${data?.enabled_count ?? 0} enabled`}
          />
          <StatCard
            label="Throttled (30d)"
            value={data?.blocked_30d ?? 0}
            hint="rate-limited requests"
          />
        </SimpleGrid>

        <Card withBorder padding="md" radius="md">
          <Title order={5} mb="sm">
            Usage (last 30 days)
          </Title>
          <div style={{ width: "100%", height: 280 }}>
            <ResponsiveContainer>
              <AreaChart data={series.data?.series ?? []}>
                <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
                <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={24} />
                <YAxis tick={{ fontSize: 11 }} allowDecimals={false} />
                <Tooltip />
                <Legend />
                <Area
                  type="monotone"
                  dataKey="search"
                  stackId="1"
                  stroke="#4dabf7"
                  fill="#4dabf7"
                  fillOpacity={0.3}
                  name="Searches"
                />
                <Area
                  type="monotone"
                  dataKey="download"
                  stackId="1"
                  stroke="#69db7c"
                  fill="#69db7c"
                  fillOpacity={0.3}
                  name="Downloads"
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </Card>

        <Card withBorder padding="md" radius="md">
          <Title order={5} mb="sm">
            Top keys (30 days)
          </Title>
          {data && data.top_keys.length > 0 ? (
            <Table>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Key</Table.Th>
                  <Table.Th>Prefix</Table.Th>
                  <Table.Th style={{ textAlign: "right" }}>Requests</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {data.top_keys.map((k) => (
                  <Table.Tr key={k.key_id}>
                    <Table.Td>{k.name}</Table.Td>
                    <Table.Td>
                      <Text size="xs" c="dimmed" ff="monospace">
                        {k.prefix}
                      </Text>
                    </Table.Td>
                    <Table.Td style={{ textAlign: "right" }}>
                      {k.total}
                    </Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          ) : (
            <Group justify="center" py="md">
              <Text c="dimmed" size="sm">
                No usage recorded yet.
              </Text>
            </Group>
          )}
        </Card>
      </Stack>
    </QueryOverlay>
  );
};

export default OverviewPanel;
