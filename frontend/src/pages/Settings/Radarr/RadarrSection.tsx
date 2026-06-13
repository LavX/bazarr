import { FunctionComponent, ReactNode } from "react";
import { Code } from "@mantine/core";
import {
  Check,
  Chips,
  CollapseBox,
  Message,
  PathMappingTable,
  Section,
  Slider,
} from "@/pages/Settings/components";
import { moviesEnabledKey } from "@/pages/Settings/keys";

interface Props {
  children?: ReactNode;
}

// Radarr controls for the Connections page: master enable toggle, instance
// cards (children), then global options and path mappings gated behind the
// toggle. Host/port/key live in the instance cards.
const RadarrSection: FunctionComponent<Props> = ({ children }) => {
  return (
    <>
      <Section header="Use Radarr">
        <Check label="Enabled" settingKey={moviesEnabledKey}></Check>
      </Section>

      {children}

      <CollapseBox settingKey={moviesEnabledKey}>
        <Section header="Options">
          <Check
            label="Sync with Radarr on live connection establishment"
            settingKey="settings-radarr-movies_sync_on_live"
          ></Check>
          <Message>
            When Bazarr connects or reconnects to Radarr, run a movies
            synchronization to make sure that we're up-to-date.
          </Message>
          <Slider
            label="Minimum Score For Movies"
            settingKey="settings-general-minimum_score_movie"
          ></Slider>
          <Chips
            label="Excluded Tags"
            settingKey="settings-radarr-excluded_tags"
            sanitizeFn={(values: string[] | null) =>
              values?.map((item) =>
                item.replace(/[^a-z0-9_-]/gi, "").toLowerCase(),
              )
            }
          ></Chips>
          <Message>
            Movies with those tags (case sensitive) in Radarr will be excluded
            from automatic download of subtitles.
          </Message>
          <Check
            label="Download Only Monitored"
            settingKey="settings-radarr-only_monitored"
          ></Check>
          <Message>
            Automatic download of subtitles will only happen for monitored
            movies in Radarr.
          </Message>
          <Check
            label="Defer searching of subtitles until scheduled task execution"
            settingKey="settings-radarr-defer_search_signalr"
          ></Check>
          <Message>
            If enabled, this option will prevent Bazarr from searching subtitles
            as soon as movies are imported.
          </Message>
          <Message>
            Search can be triggered using this command
            <Code>
              {`curl -H "Content-Type: application/json" -H "X-API-KEY: ###############################" -X POST
                -d '{ "eventType": "Download", "movieFile": { "id": "$radarr_moviefile_id" } }'
                http://localhost:6767/api/webhooks/radarr
              `}
            </Code>
          </Message>
        </Section>
        <Section header="Path Mappings">
          <PathMappingTable type="radarr"></PathMappingTable>
        </Section>
      </CollapseBox>
    </>
  );
};

export default RadarrSection;
