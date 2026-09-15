import { FunctionComponent } from "react";
import { Group, Progress, Stack, Table, Text } from "@mantine/core";
import {
  Bar,
  BarChart,
  CartesianGrid,
  LabelList,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useHistoryMetrics } from "@/apis/hooks";
import { QueryOverlay } from "@/components/async";
import PanelCard from "./components/PanelCard";
import { StatisticsFilters } from "./filters";

interface Props {
  filters: StatisticsFilters;
}

/** Colour the reliability bar by how bad the rate is. */
const rateColor = (pct: number) =>
  pct >= 10 ? "#fa5252" : pct >= 3 ? "#fab005" : "#40c057";

const ProvidersPanel: FunctionComponent<Props> = ({ filters }) => {
  const query = useHistoryMetrics(
    filters.timeFrame,
    filters.action,
    filters.provider,
    filters.language,
  );
  const metrics = query.data;
  const leaderboard = metrics?.byProvider ?? [];
  const reliability = metrics?.providerReliability ?? [];

  // Height scales with the row count so ten providers do not squash into the
  // same box as two.
  const chartHeight = Math.max(200, leaderboard.length * 38 + 40);

  return (
    <QueryOverlay result={query}>
      <Stack gap="lg">
        <PanelCard
          title="Downloads by provider"
          caveat="Which providers actually feed your library over the selected window."
        >
          {leaderboard.length === 0 ? (
            <Text size="sm" c="dimmed">
              No downloads in this period.
            </Text>
          ) : (
            <div style={{ width: "100%", height: chartHeight }}>
              <ResponsiveContainer>
                <BarChart
                  data={leaderboard}
                  layout="vertical"
                  margin={{ left: 8, right: 48 }}
                >
                  <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
                  <XAxis
                    type="number"
                    tick={{ fontSize: 11 }}
                    allowDecimals={false}
                  />
                  <YAxis
                    type="category"
                    dataKey="provider"
                    width={140}
                    tick={{ fontSize: 11 }}
                  />
                  <Tooltip />
                  <Bar dataKey="count" name="Downloads" fill="#4dabf7">
                    <LabelList
                      dataKey="count"
                      position="right"
                      style={{ fontSize: 11 }}
                    />
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}
        </PanelCard>

        <PanelCard
          title="Match quality by provider"
          caveat="Mean score as a percentage of what was achievable, normalised so episodes and movies are comparable. A hash match can exceed 100%."
        >
          {leaderboard.length === 0 ? (
            <Text size="sm" c="dimmed">
              Nothing scored in this period.
            </Text>
          ) : (
            <Table>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Provider</Table.Th>
                  <Table.Th>Downloads</Table.Th>
                  <Table.Th>Mean quality</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {leaderboard.map((row) => (
                  <Table.Tr key={row.provider}>
                    <Table.Td>
                      <Text size="sm">{row.provider}</Text>
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm" c="dimmed">
                        {row.count}
                      </Text>
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm">
                        {row.avgScorePct === null
                          ? "-"
                          : `${row.avgScorePct.toFixed(1)}%`}
                      </Text>
                    </Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          )}
        </PanelCard>

        <PanelCard
          title="Blacklist rate"
          caveat="Share of this provider's downloads that you later blacklisted. A provider high on both charts is the one to turn off."
        >
          {reliability.length === 0 ? (
            <Text size="sm" c="dimmed">
              No downloads to rate yet.
            </Text>
          ) : (
            <Stack gap="sm">
              {reliability.map((row) => (
                <div key={row.provider}>
                  <Group justify="space-between" gap="xs">
                    <Text size="sm">{row.provider}</Text>
                    <Text size="sm" c="dimmed">
                      {row.ratePct.toFixed(1)}% ({row.blacklisted} of{" "}
                      {row.downloads})
                    </Text>
                  </Group>
                  <Progress
                    mt={4}
                    value={Math.min(100, row.ratePct)}
                    color={rateColor(row.ratePct)}
                  />
                </div>
              ))}
            </Stack>
          )}
        </PanelCard>
      </Stack>
    </QueryOverlay>
  );
};

export default ProvidersPanel;
