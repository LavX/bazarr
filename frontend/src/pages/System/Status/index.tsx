import {
  FunctionComponent,
  JSX,
  PropsWithChildren,
  ReactNode,
  useCallback,
  useMemo,
  useState,
} from "react";
import {
  ActionIcon,
  Anchor,
  Container,
  Divider,
  Grid,
  Group,
  Space,
  Stack,
  Text,
  Tooltip,
} from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import { IconDefinition } from "@fortawesome/fontawesome-common-types";
import {
  faDiscord,
  faFontAwesome,
  faGithub,
  faWikipediaW,
} from "@fortawesome/free-brands-svg-icons";
import {
  faCode,
  faPaperPlane,
  faRefresh,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useSystemHealth, useSystemStatus } from "@/apis/hooks";
import { useArrKindAvailability } from "@/apis/hooks/arrInstances";
import { useAppTitle } from "@/apis/hooks/site";
import api from "@/apis/raw";
import { QueryOverlay } from "@/components/async";
import {
  hasWhatsNew,
  useOpenWhatsNew,
} from "@/components/modals/useWhatsNewAutoOpen";
import { GithubRepoRoot } from "@/constants";
import { Environment, useInterval } from "@/utilities";
import {
  divisorDay,
  divisorHour,
  divisorMinute,
  divisorSecond,
  formatTime,
} from "@/utilities/time";
import Table from "./table";

interface InfoProps {
  title: string;
  children: ReactNode;
}

function Row(props: InfoProps): JSX.Element {
  const { title, children } = props;
  return (
    <Grid columns={10}>
      <Grid.Col span={{ base: 4, sm: 2 }} style={{ minWidth: 0 }}>
        <Text
          size="sm"
          ta="right"
          fw="bold"
          style={{ overflowWrap: "anywhere" }}
        >
          {title}
        </Text>
      </Grid.Col>
      <Grid.Col span={{ base: 6, sm: 3 }} style={{ minWidth: 0 }}>
        <Text component="div" size="sm" style={{ overflowWrap: "anywhere" }}>
          {" "}
          {children}
        </Text>
      </Grid.Col>
    </Grid>
  );
}

interface IconProps {
  icon: IconDefinition;
  link: string;
  children: string;
}

function Label(props: IconProps): JSX.Element {
  const { icon, link, children } = props;
  return (
    <>
      <FontAwesomeIcon icon={icon} style={{ width: "2rem" }}></FontAwesomeIcon>
      <Anchor href={link} target="_blank" rel="noopener noreferrer">
        {children}
      </Anchor>
    </>
  );
}

// What each media server product is called in the UI. The kinds come from the
// destination layer, which spells them lowercase.
const mediaServerLabels: Record<System.MediaServerStatus["kind"], string> = {
  emby: "Emby",
  jellyfin: "Jellyfin",
  plex: "Plex",
  silo: "Silo",
};

function mediaServerTitle(
  server: System.MediaServerStatus,
  sameKind: number,
): string {
  const label = mediaServerLabels[server.kind] ?? server.kind;
  // One Emby reads best as plain "Emby Version", matching the Sonarr and
  // Radarr rows above it. Name the instance only once there is more than one
  // of that product to tell apart.
  return sameKind > 1
    ? `${label} Version (${server.name})`
    : `${label} Version`;
}

function mediaServerValue(server: System.MediaServerStatus): string {
  if (server.state === "checking") {
    // No probe has answered yet. The page polls until one does.
    return "Checking…";
  }
  if (server.state === "unreachable") {
    return "Unreachable";
  }
  // Silo's health endpoint reports an id and a name and no version at all, so
  // saying so is the honest answer rather than an empty cell.
  return server.version || "Connected, no version reported";
}

// How soon to ask again while a probe is in flight. A probe is one bounded
// request against one server, so the answer lands within a few seconds.
const probePollMs = 4000;

// The timer still has to tick when nothing is pending, because the hook takes
// a number. The callback declines to fetch, so this costs one no-op.
const idlePollMs = 60000;

function mediaServerPollDelay(server: System.MediaServerStatus): number {
  if (server.state === "checking" || server.refresh_in === 0) {
    // A probe is running: the state on screen is the old answer, or no
    // answer, and the real one is seconds away.
    return 0;
  }
  if (server.refresh_in === undefined) {
    // A payload that predates the field says nothing about when this could
    // change, and guessing "now" would poll a settled page forever.
    return Number.POSITIVE_INFINITY;
  }
  // A failed probe is retried on its own short schedule and a recovered
  // server reports its version again, so this covers a server coming back up
  // as much as it covers a version changing.
  return server.refresh_in * 1000;
}

interface InfoContainerProps {
  title: string;
}

const InfoContainer: FunctionComponent<
  PropsWithChildren<InfoContainerProps>
> = ({ title, children }) => {
  return (
    <Stack>
      <Divider
        labelPosition="left"
        label={
          <Text size="md" fw="bold">
            {title}
          </Text>
        }
      ></Divider>
      {children}
      <Space />
    </Stack>
  );
};

const SystemStatusView: FunctionComponent = () => {
  const health = useSystemHealth();
  const statusQuery = useSystemStatus();
  const { data: status } = statusQuery;
  const { enabled: sonarrEnabled } = useArrKindAvailability("sonarr");
  const { enabled: radarrEnabled } = useArrKindAvailability("radarr");
  const { enabled: sportsEnabled } = useArrKindAvailability("sportarr");
  const openWhatsNew = useOpenWhatsNew();
  const showWhatsNew = hasWhatsNew();

  const mediaServers = useMemo(
    () => status?.media_servers ?? [],
    [status?.media_servers],
  );
  const perKind = useMemo(() => {
    const counts = new Map<string, number>();
    mediaServers.forEach((server) =>
      counts.set(server.kind, (counts.get(server.kind) ?? 0) + 1),
    );
    return counts;
  }, [mediaServers]);

  // Versions resolve in the background, so the page asks again when there
  // could be something new: while a probe is running, and once a cached
  // answer has expired. Between those it waits, because every request in
  // that window returns exactly what the page already shows.
  const pollDelay = useMemo(() => {
    const soonest = Math.min(...mediaServers.map(mediaServerPollDelay));
    return Number.isFinite(soonest) ? Math.max(soonest, probePollMs) : null;
  }, [mediaServers]);
  const { refetch: refetchStatus } = statusQuery;
  const pollVersions = useCallback(() => {
    if (pollDelay !== null) {
      void refetchStatus();
    }
  }, [pollDelay, refetchStatus]);
  useInterval(pollVersions, pollDelay ?? idlePollMs);

  const [uptime, setUptime] = useState<string>();

  const update = useCallback(() => {
    const startTime = status?.start_time;
    if (startTime) {
      // Current time in seconds
      const currentTime = Math.floor(Date.now() / 1000);

      const uptimeInSeconds = currentTime - startTime;

      const uptime: string = formatTime(uptimeInSeconds, [
        { unit: "d", divisor: divisorDay },
        { unit: "h", divisor: divisorHour },
        { unit: "m", divisor: divisorMinute },
        { unit: "s", divisor: divisorSecond },
      ]);

      setUptime(uptime);
    }
  }, [status?.start_time]);

  useInterval(update, 1000);

  useDocumentTitle(`Status - ${useAppTitle()} (System)`);

  return (
    <Container fluid>
      <Stack>
        <Stack>
          <Divider
            labelPosition="left"
            label={
              <Group gap="xs">
                <Text size="md" fw="bold">
                  Health
                </Text>
                <Tooltip label="Re-check health" position="right">
                  <ActionIcon
                    variant="subtle"
                    size="sm"
                    onClick={async () => {
                      await api.system.recheckHealth();
                      health.refetch();
                    }}
                    loading={health.isFetching}
                  >
                    <FontAwesomeIcon icon={faRefresh} size="sm" />
                  </ActionIcon>
                </Tooltip>
              </Group>
            }
          ></Divider>
          <QueryOverlay result={health}>
            <Table health={health.data ?? []}></Table>
          </QueryOverlay>
          <Space />
        </Stack>
        <InfoContainer title="About">
          <Row title="Bazarr Version">
            <Group gap="sm">
              {status?.bazarr_version}
              {showWhatsNew && (
                <Anchor component="button" type="button" onClick={openWhatsNew}>
                  What&apos;s new
                </Anchor>
              )}
            </Group>
          </Row>
          {status?.package_version !== "" && (
            <Row title="Package Version">{status?.package_version}</Row>
          )}
          {/* Every integration row is conditional, on the rule Sportarr has
              used since sports landed: the product's master toggle is on and
              there is an enabled instance to query. An unconfigured Sonarr
              used to render an empty row here, which reads as broken rather
              than as absent. The version itself has to be present too, so a
              row never appears before the endpoint has answered. */}
          {sonarrEnabled && status?.sonarr_version ? (
            <Row title="Sonarr Version">{status.sonarr_version}</Row>
          ) : null}
          {radarrEnabled && status?.radarr_version ? (
            <Row title="Radarr Version">{status.radarr_version}</Row>
          ) : null}
          {sportsEnabled && status?.sportarr_version ? (
            <Row title="Sportarr Version">{status.sportarr_version}</Row>
          ) : null}
          {/* Media servers were never on this page at all, though a connected
              server's version is the first thing anyone checks when refreshes
              misbehave. One row per instance, because these are
              multi-instance: two Embys are two rows. */}
          {mediaServers.map((server) => (
            <Row
              key={server.id}
              title={mediaServerTitle(server, perKind.get(server.kind) ?? 1)}
            >
              {mediaServerValue(server)}
            </Row>
          ))}
          <Row title="Operating System">{status?.operating_system}</Row>
          <Row title="Python Version">{status?.python_version}</Row>
          <Row title="Database Engine">{status?.database_engine}</Row>
          <Row title="Database Version">{status?.database_migration}</Row>
          <Row title="Bazarr Directory">{status?.bazarr_directory}</Row>
          <Row title="Bazarr Config Directory">
            {status?.bazarr_config_directory}
          </Row>
          <Row title="Uptime">{uptime}</Row>
          <Row title="Time Zone">{status?.timezone}</Row>
        </InfoContainer>
        <InfoContainer title="More Info">
          <Row title="Home Page">
            <Label icon={faPaperPlane} link="https://lavx.github.io/bazarr">
              Bazarr+ Website
            </Label>
          </Row>
          <Row title="Source">
            <Label icon={faGithub} link={GithubRepoRoot}>
              Bazarr+ on GitHub
            </Label>
          </Row>
          <Row title="Wiki">
            <Label
              icon={faWikipediaW}
              link="https://lavx.github.io/bazarr/guides/"
            >
              Bazarr+ Wiki
            </Label>
          </Row>
          <Row title="API documentation">
            <Label icon={faCode} link={`${Environment.baseUrl}/api/`}>
              Swagger UI
            </Label>
          </Row>
          <Row title="Community">
            <Label icon={faDiscord} link="https://discord.gg/fpp5JXmB8f">
              Bazarr+ on Discord
            </Label>
          </Row>
        </InfoContainer>
        <InfoContainer title="Credits">
          <Row title="TMDB">
            <Label icon={faPaperPlane} link="https://www.themoviedb.org/">
              Movie and TV metadata
            </Label>
          </Row>
          <Row title="TheTVDB">
            <Label icon={faPaperPlane} link="https://thetvdb.com">
              TV series metadata
            </Label>
          </Row>
          <Row title="OMDb">
            <Label icon={faPaperPlane} link="https://www.omdbapi.com">
              Movie metadata fallback
            </Label>
          </Row>
          <Row title="Apprise">
            <Label icon={faGithub} link="https://github.com/caronc/apprise">
              Notification backend by caronc
            </Label>
          </Row>
          <Row title="Font Awesome">
            <Label icon={faFontAwesome} link="https://fontawesome.com">
              Icons under CC BY 4.0
            </Label>
          </Row>
        </InfoContainer>
      </Stack>
    </Container>
  );
};

export default SystemStatusView;
