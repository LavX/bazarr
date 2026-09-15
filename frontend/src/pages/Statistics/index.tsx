import { FunctionComponent, useState } from "react";
import { Container, Tabs } from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import {
  faChartColumn,
  faLanguage,
  faTowerBroadcast,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useDistSettings } from "@/apis/hooks";
import { useAppTitle } from "@/apis/hooks/site";
import DistributionHubOverview from "@/pages/DistributionHub/OverviewPanel";
import ActivityPanel from "./ActivityPanel";
import TranslatorPanel from "./TranslatorPanel";

type TabKey = "activity" | "distribution" | "translator";

const StatisticsView: FunctionComponent = () => {
  const [tab, setTab] = useState<TabKey>("activity");
  const { data: distSettings } = useDistSettings();

  useDocumentTitle(`Statistics - ${useAppTitle()} (System)`);

  // Hide the distribution tab while the endpoint is off: compat_usage is only
  // written when it is serving, so the chart would be a flat zero line that
  // reads as "usage stopped" rather than "never started".
  const showDistribution = distSettings?.enabled ?? false;

  return (
    <Container fluid px="md">
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

        <Tabs.Panel value="activity" pt="md">
          <ActivityPanel />
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
    </Container>
  );
};

export default StatisticsView;
