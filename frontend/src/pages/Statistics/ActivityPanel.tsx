import { FunctionComponent, useMemo, useState } from "react";
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
import {
  useHistoryStats,
  useLanguages,
  useSystemProviders,
} from "@/apis/hooks";
import { useSportsAvailability } from "@/apis/hooks/sports";
import { Selector } from "@/components";
import { QueryOverlay } from "@/components/async";
import { useSelectorOptions } from "@/utilities";
import PanelCard from "./components/PanelCard";
import { actionOptions, timeFrameOptions } from "./options";

const ActivityPanel: FunctionComponent = () => {
  // history=true lists the providers that actually appear in download
  // history, which is the useful set to filter by here.
  const { data: providers } = useSystemProviders(true);
  const providerOptions = useSelectorOptions(providers ?? [], (v) => v.name);

  const { data: historyLanguages } = useLanguages(true);
  const languageOptions = useSelectorOptions(
    historyLanguages ?? [],
    (value) => value.name,
  );

  const [timeFrame, setTimeFrame] = useState<History.TimeFrameOptions>("month");
  const [action, setAction] = useState<Nullable<History.ActionOptions>>(null);
  const [lang, setLanguage] = useState<Nullable<Language.Server>>(null);
  const [provider, setProvider] = useState<Nullable<System.Provider>>(null);

  const { enabled: sportsEnabled } = useSportsAvailability();
  const stats = useHistoryStats(timeFrame, action, provider, lang);
  const { data } = stats;

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
        <Selector
          placeholder="Time..."
          options={timeFrameOptions}
          value={timeFrame}
          onChange={(v) => setTimeFrame(v ?? "month")}
        ></Selector>
        <Selector
          placeholder="Action..."
          clearable
          options={actionOptions}
          value={action}
          onChange={setAction}
        ></Selector>
        <Selector
          {...providerOptions}
          placeholder="Provider..."
          clearable
          value={provider}
          onChange={setProvider}
        ></Selector>
        <Selector
          {...languageOptions}
          placeholder="Language..."
          clearable
          value={lang}
          onChange={setLanguage}
        ></Selector>
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
