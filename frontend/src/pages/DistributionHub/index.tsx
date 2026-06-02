import { FunctionComponent, useState } from "react";
import { Alert, Button, Container, Group, Tabs } from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import {
  faChartLine,
  faGears,
  faKey,
  faLayerGroup,
  faTriangleExclamation,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useDistSaveSettings, useDistSettings } from "@/apis/hooks";
import { useInstanceName } from "@/apis/hooks/site";
import KeysPanel from "./KeysPanel";
import OverviewPanel from "./OverviewPanel";
import SettingsPanel from "./SettingsPanel";
import TiersPanel from "./TiersPanel";

type TabKey = "overview" | "keys" | "tiers" | "settings";

const DistributionHubView: FunctionComponent = () => {
  const [tab, setTab] = useState<TabKey>("overview");
  const settings = useDistSettings();
  const saveSettings = useDistSaveSettings();

  useDocumentTitle(`Distribution Hub - ${useInstanceName()}`);

  const enabled = settings.data?.enabled ?? true;

  return (
    <Container fluid px="md">
      {settings.data && !enabled && (
        <Alert
          mb="md"
          color="yellow"
          icon={<FontAwesomeIcon icon={faTriangleExclamation} />}
          title="Distribution Hub is disabled"
        >
          <Group justify="space-between" align="center">
            <span>
              Turn it on to serve subtitles to your apps and websites through
              the OpenSubtitles-compatible API.
            </span>
            <Button
              size="xs"
              loading={saveSettings.isPending}
              onClick={() =>
                saveSettings.mutate({ enabled: true, consent: true })
              }
            >
              Enable now
            </Button>
          </Group>
        </Alert>
      )}

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
          <Tabs.Tab value="keys" leftSection={<FontAwesomeIcon icon={faKey} />}>
            API Keys
          </Tabs.Tab>
          <Tabs.Tab
            value="tiers"
            leftSection={<FontAwesomeIcon icon={faLayerGroup} />}
          >
            Tiers
          </Tabs.Tab>
          <Tabs.Tab
            value="settings"
            leftSection={<FontAwesomeIcon icon={faGears} />}
          >
            Settings
          </Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="overview" pt="md">
          <OverviewPanel />
        </Tabs.Panel>
        <Tabs.Panel value="keys" pt="md">
          <KeysPanel />
        </Tabs.Panel>
        <Tabs.Panel value="tiers" pt="md">
          <TiersPanel />
        </Tabs.Panel>
        <Tabs.Panel value="settings" pt="md">
          <SettingsPanel />
        </Tabs.Panel>
      </Tabs>
    </Container>
  );
};

export default DistributionHubView;
