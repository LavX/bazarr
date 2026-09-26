import { FunctionComponent, useState } from "react";
import { Container, Stack, Tabs } from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import {
  faChartColumn,
  faLanguage,
  faPlug,
  faStar,
  faTowerBroadcast,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useDistSettings } from "@/apis/hooks";
import { useAppTitle } from "@/apis/hooks/site";
import DistributionHubOverview from "@/pages/DistributionHub/OverviewPanel";
import MetricsFilters from "./components/MetricsFilters";
import ActivityPanel from "./ActivityPanel";
import { defaultStatisticsFilters, StatisticsFilters } from "./filters";
import ProvidersPanel from "./ProvidersPanel";
import QualityPanel from "./QualityPanel";
import TranslatorPanel from "./TranslatorPanel";

type TabKey =
  | "activity"
  | "providers"
  | "quality"
  | "distribution"
  | "translator";

/** Tabs driven by the history filters. Distribution and Translator are not. */
const FILTERED_TABS: TabKey[] = ["activity", "providers", "quality"];

const StatisticsView: FunctionComponent = () => {
  const [tab, setTab] = useState<TabKey>("activity");
  const [filters, setFilters] = useState<StatisticsFilters>(
    defaultStatisticsFilters,
  );
  const { data: distSettings } = useDistSettings();

  useDocumentTitle(`Statistics - ${useAppTitle()} (System)`);

  // Hide the distribution tab while the endpoint is off: compat_usage is only
  // written when it is serving, so the chart would be a flat zero line that
  // reads as "usage stopped" rather than "never started".
  const showDistribution = distSettings?.enabled ?? false;

  return (
    <Container fluid px="md">
      <Stack gap="md">
        <Tabs
          value={tab}
          onChange={(v) => v && setTab(v as TabKey)}
          keepMounted={false}
        >
          <Tabs.List>
            <Tabs.Tab
              value="activity"
              leftSection={<FontAwesomeIcon icon={faChartColumn} />}
            >
              Activity
            </Tabs.Tab>
            <Tabs.Tab
              value="providers"
              leftSection={<FontAwesomeIcon icon={faPlug} />}
            >
              Providers
            </Tabs.Tab>
            <Tabs.Tab
              value="quality"
              leftSection={<FontAwesomeIcon icon={faStar} />}
            >
              Quality
            </Tabs.Tab>
            {showDistribution && (
              <Tabs.Tab
                value="distribution"
                leftSection={<FontAwesomeIcon icon={faTowerBroadcast} />}
              >
                Distribution
              </Tabs.Tab>
            )}
            <Tabs.Tab
              value="translator"
              leftSection={<FontAwesomeIcon icon={faLanguage} />}
            >
              Translator
            </Tabs.Tab>
          </Tabs.List>

          {/* One filter row for every history-driven tab, so narrowing to a
              provider and then switching tabs keeps that provider selected. */}
          {FILTERED_TABS.includes(tab) && (
            <Container fluid px={0} pt="md">
              <MetricsFilters value={filters} onChange={setFilters} />
            </Container>
          )}

          <Tabs.Panel value="activity" pt="md">
            <ActivityPanel filters={filters} />
          </Tabs.Panel>
          <Tabs.Panel value="providers" pt="md">
            <ProvidersPanel filters={filters} />
          </Tabs.Panel>
          <Tabs.Panel value="quality" pt="md">
            <QualityPanel filters={filters} />
          </Tabs.Panel>
          {showDistribution && (
            <Tabs.Panel value="distribution" pt="md">
              <DistributionHubOverview />
            </Tabs.Panel>
          )}
          <Tabs.Panel value="translator" pt="md">
            <TranslatorPanel />
          </Tabs.Panel>
        </Tabs>
      </Stack>
    </Container>
  );
};

export default StatisticsView;
