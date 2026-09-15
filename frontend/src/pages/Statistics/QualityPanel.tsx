import { FunctionComponent, useMemo } from "react";
import { SimpleGrid, Stack, Table, Text } from "@mantine/core";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useHistoryMetrics } from "@/apis/hooks";
import { QueryOverlay } from "@/components/async";
import PanelCard from "./components/PanelCard";
import { actionLabel, StatisticsFilters } from "./filters";

interface Props {
  filters: StatisticsFilters;
}

/** The last bucket holds everything at or above 100%, where hash matches land. */
const OVERFLOW_BUCKET = 10;

const bucketLabel = (bucket: number) =>
  bucket === OVERFLOW_BUCKET ? "100%+" : `${bucket * 10}-${bucket * 10 + 9}%`;

const QualityPanel: FunctionComponent<Props> = ({ filters }) => {
  const query = useHistoryMetrics(
    filters.timeFrame,
    filters.action,
    filters.provider,
    filters.language,
  );
  const metrics = query.data;

  const histogram = useMemo(
    () =>
      (metrics?.scoreHistogram ?? []).map((row) => ({
        ...row,
        label: bucketLabel(row.bucket),
      })),
    [metrics],
  );
  const scored = histogram.reduce((sum, row) => sum + row.count, 0);

  const languages = metrics?.byLanguage ?? [];
  const actions = metrics?.byAction ?? [];

  return (
    <QueryOverlay result={query}>
      <Stack gap="lg">
        <PanelCard
          title="Match quality distribution"
          caveat="Score as a percentage of what was achievable. Uploads, embedded tracks and translations are excluded, because their score is either hardcoded perfect or derived from a setting. The 100%+ bucket is a hash match, which scores above the denominator by design."
        >
          {scored === 0 ? (
            <Text size="sm" c="dimmed">
              Nothing scored in this period.
            </Text>
          ) : (
            <div style={{ width: "100%", height: 300 }}>
              <ResponsiveContainer>
                <BarChart data={histogram}>
                  <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
                  <XAxis dataKey="label" tick={{ fontSize: 11 }} />
                  <YAxis tick={{ fontSize: 11 }} allowDecimals={false} />
                  <Tooltip />
                  <Bar dataKey="count" name="Downloads">
                    {histogram.map((row) => (
                      <Cell
                        key={row.bucket}
                        // The overflow bucket is a different kind of result,
                        // not simply a better score, so it reads differently.
                        fill={
                          row.bucket === OVERFLOW_BUCKET ? "#40c057" : "#4dabf7"
                        }
                      />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}
        </PanelCard>

        <SimpleGrid cols={{ base: 1, lg: 2 }}>
          <PanelCard
            title="Languages downloaded"
            caveat="A :hi or :forced suffix is a separate variant, counted on its own."
          >
            {languages.length === 0 ? (
              <Text size="sm" c="dimmed">
                No downloads in this period.
              </Text>
            ) : (
              <Table>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Language</Table.Th>
                    <Table.Th>Downloads</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {languages.map((row) => (
                    <Table.Tr key={row.language}>
                      <Table.Td>
                        <Text size="sm">{row.language}</Text>
                      </Table.Td>
                      <Table.Td>
                        <Text size="sm" c="dimmed">
                          {row.count}
                        </Text>
                      </Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            )}
          </PanelCard>

          <PanelCard
            title="How subtitles arrived"
            caveat="A high upgrade share means the first match often was not good enough."
          >
            {actions.length === 0 ? (
              <Text size="sm" c="dimmed">
                No downloads in this period.
              </Text>
            ) : (
              <Table>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>How</Table.Th>
                    <Table.Th>Downloads</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {actions.map((row) => (
                    <Table.Tr key={row.action}>
                      <Table.Td>
                        <Text size="sm">{actionLabel(row.action)}</Text>
                      </Table.Td>
                      <Table.Td>
                        <Text size="sm" c="dimmed">
                          {row.count}
                        </Text>
                      </Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            )}
          </PanelCard>
        </SimpleGrid>
      </Stack>
    </QueryOverlay>
  );
};

export default QualityPanel;
