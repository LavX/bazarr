import { FunctionComponent, useMemo } from "react";
import { SimpleGrid, Stack, useMantineTheme } from "@mantine/core";
import { merge } from "lodash";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useHistoryMetrics, useHistoryStats } from "@/apis/hooks";
import { useSportsAvailability } from "@/apis/hooks/sports";
import { QueryOverlay } from "@/components/async";
import PanelCard from "./components/PanelCard";
import StatTile from "./components/StatTile";
import { StatisticsFilters } from "./filters";

interface Props {
  filters: StatisticsFilters;
}

const ActivityPanel: FunctionComponent<Props> = ({ filters }) => {
  const { timeFrame, action, provider, language: lang } = filters;

  const { enabled: sportsEnabled } = useSportsAvailability();
  const stats = useHistoryStats(timeFrame, action, provider, lang);
  const metrics = useHistoryMetrics(timeFrame, action, provider, lang);
  const { data } = stats;
  const totals = metrics.data?.totals;

  const convertedData = useMemo(() => {
    if (!data) return [];

    const movies = data.movies.map((v) => ({ date: v.date, movies: v.count }));
    const series = data.series.map((v) => ({ date: v.date, series: v.count }));
    // The endpoint has counted sports downloads all along; plotting only two
    // of the three series it returns would drop them silently.
    const sports = (data.sports ?? []).map((v) => ({
      date: v.date,
      sports: v.count,
    }));
    return merge(merge(movies, series), sports);
  }, [data]);

  const theme = useMantineTheme();

  return (
    <Stack gap="lg">
      <SimpleGrid cols={{ base: 1, xs: 2, lg: 4 }}>
        <StatTile
          label="Downloads"
          value={totals?.downloads ?? 0}
          hint={
            totals
              ? `${totals.series} series / ${totals.movies} movies${
                  sportsEnabled ? ` / ${totals.sports} sports` : ""
                }`
              : undefined
          }
        />
        <StatTile
          label="Per day"
          value={totals?.dailyAverage ?? 0}
          hint="average across the window"
        />
        <StatTile
          label="Busiest day"
          value={totals?.peakCount ?? 0}
          hint={totals?.peakDate ?? "nothing yet"}
        />
        <StatTile
          label="Arrived automatically"
          value={`${totals?.automaticPct ?? 0}%`}
          hint="no one had to search"
        />
      </SimpleGrid>

      <PanelCard
        title="Subtitles downloaded per day"
        caveat="Automatic, manual and upgraded downloads. Removing media from Sonarr or Radarr deletes its history, so older days can shrink over time."
      >
        <QueryOverlay result={stats}>
          <div style={{ width: "100%", height: 360 }}>
            <ResponsiveContainer>
              <BarChart data={convertedData}>
                <CartesianGrid strokeDasharray="4 2" opacity={0.2} />
                <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={24} />
                <YAxis tick={{ fontSize: 11 }} allowDecimals={false} />
                <Tooltip />
                <Legend verticalAlign="top" />
                <Bar
                  name="Series"
                  dataKey="series"
                  fill={theme.colors.blue[4]}
                />
                <Bar
                  name="Movies"
                  dataKey="movies"
                  fill={theme.colors.yellow[4]}
                />
                {sportsEnabled && (
                  <Bar
                    name="Sports"
                    dataKey="sports"
                    fill={theme.colors.teal[4]}
                  />
                )}
              </BarChart>
            </ResponsiveContainer>
          </div>
        </QueryOverlay>
      </PanelCard>
    </Stack>
  );
};

export default ActivityPanel;
