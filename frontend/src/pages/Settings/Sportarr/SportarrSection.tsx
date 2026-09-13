import { FunctionComponent, ReactNode } from "react";
import {
  Check,
  Chips,
  CollapseBox,
  Message,
  PathMappingTable,
  Section,
  Slider,
} from "@/pages/Settings/components";
import { sportsEnabledKey } from "@/pages/Settings/keys";

interface Props {
  // The instance cards, rendered between the master toggle and the gated
  // options so they stay visible regardless of the "Use Sportarr" state.
  children?: ReactNode;
}

// Sportarr controls for the Connections page, mirroring SonarrSection and
// RadarrSection: master enable toggle, the instance cards (passed as children),
// then the global behavioural options and path mappings gated behind the
// toggle. Connection details live in the instance cards, so there is no Host
// section here. Sync cadence and full-scan settings live in Scheduler settings,
// where their Sonarr and Radarr counterparts already are.
const SportarrSection: FunctionComponent<Props> = ({ children }) => {
  return (
    <>
      <Section header="Use Sportarr">
        <Check label="Enabled" settingKey={sportsEnabledKey}></Check>
      </Section>

      {children}

      <CollapseBox settingKey={sportsEnabledKey}>
        <Section header="Options">
          <Slider
            label="Minimum Score For Sports Events"
            settingKey="settings-general-minimum_score_sports"
          ></Slider>
          <Message>
            Sports events are scored on the same scale as movies.
          </Message>
          <Chips
            label="Excluded Tags"
            settingKey="settings-sportarr-excluded_tags"
            sanitizeFn={(values: string[] | null) =>
              values?.map((item) =>
                item.replace(/[^a-z0-9_-]/gi, "").toLowerCase(),
              )
            }
          ></Chips>
          <Message>
            Events from leagues with those tags (case sensitive) in Sportarr
            will be excluded from automatic download of subtitles.
          </Message>
          <Chips
            label="Excluded Sports"
            settingKey="settings-sportarr-excluded_sports"
          ></Chips>
          <Message>
            Events belonging to those sports will be excluded from automatic
            download of subtitles.
          </Message>
          <Check
            label="Download Only Monitored"
            settingKey="settings-sportarr-only_monitored"
          ></Check>
          <Message>
            Automatic download of subtitles will only happen for monitored
            events in Sportarr.
          </Message>
          <Check
            label="Search After Sync"
            settingKey="settings-sportarr-search_on_sync"
          ></Check>
          <Message>
            If enabled, Bazarr searches for subtitles as soon as a library sync
            adds new events, instead of waiting for the scheduled search.
          </Message>
        </Section>
        <Section header="Path Mappings">
          <PathMappingTable type="sports"></PathMappingTable>
        </Section>
      </CollapseBox>
    </>
  );
};

export default SportarrSection;
