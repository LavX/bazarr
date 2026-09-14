import { FunctionComponent, useState } from "react";
import { Container, Tabs } from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import {
  faChartLine,
  faLanguage,
  faListCheck,
  faPlug,
  faTowerBroadcast,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useDistSettings } from "@/apis/hooks";
import { useInstanceName } from "@/apis/hooks/site";
import DistributionHubOverview from "@/pages/DistributionHub/OverviewPanel";
import OverviewPanel from "./OverviewPanel";
import ProvidersPanel from "./ProvidersPanel";
import TasksPanel from "./TasksPanel";
import TranslatorPanel from "./TranslatorPanel";

type TabKey =
  | "overview"
  | "providers"
  | "tasks"
  | "distribution"
  | "translator";

const StatisticsView: FunctionComponent = () => {
  const [tab, setTab] = useState<TabKey>("overview");
  const { data: distSettings } = useDistSettings();

  useDocumentTitle(`Statistics - ${useInstanceName()}`);

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
            value="overview"
            leftSection={<FontAwesomeIcon icon={faChartLine} />}
          >
            Overview
          </Tabs.Tab>
          <Tabs.Tab
            value="providers"
            leftSection={<FontAwesomeIcon icon={faPlug} />}
          >
            Providers
          </Tabs.Tab>
          <Tabs.Tab
            value="tasks"
            leftSection={<FontAwesomeIcon icon={faListCheck} />}
          >
            Tasks
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

        <Tabs.Panel value="overview" pt="md">
          <OverviewPanel />
        </Tabs.Panel>
        <Tabs.Panel value="providers" pt="md">
          <ProvidersPanel />
        </Tabs.Panel>
        <Tabs.Panel value="tasks" pt="md">
          <TasksPanel />
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
