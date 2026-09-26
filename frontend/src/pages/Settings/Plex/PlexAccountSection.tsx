import { FunctionComponent } from "react";
import { Link } from "react-router";
import { Box, Code, Paper, Text } from "@mantine/core";
import { Check, CollapseBox, Section } from "@/pages/Settings/components";
import { plexEnabledKey } from "@/pages/Settings/keys";
import AutopulseSelector from "./AutopulseSelector";
import LibrarySelector from "./LibrarySelector";
import PlexSettings from "./PlexSettings";
import WebhookSelector from "./WebhookSelector";

// Everything on the Plex tab that is not a subtitle refresh. Refreshes are
// instance rows on the shared media-server layer above this, with their own
// URL, token and libraries; what is left here is the Plex account itself, the
// recently-added dates Bazarr writes back, and the webhook and Autopulse
// helpers, all of which still read the scalar settings.
const PlexAccountSection: FunctionComponent = () => {
  return (
    <CollapseBox settingKey={plexEnabledKey}>
      <Paper p="xl">
        <Box>
          <PlexSettings />
        </Box>
      </Paper>

      {/* Which libraries the recently-added write looks the item up in.
          Subtitle refreshes are configured on the Plex instance above and use
          that instance's own libraries. */}
      <Section header="Recently added dates, movies">
        <LibrarySelector
          label="Library Name"
          settingKey="settings-plex-movie_library"
          settingKeyIds="settings-plex-movie_library_ids"
          libraryType="movie"
          description="Where to look the movie up when marking it recently added. Subtitle refreshes use the libraries on the Plex instance above."
        />
        <Check
          label="Mark movies as recently added after downloading subtitles"
          settingKey="settings-plex-set_movie_added"
        />
      </Section>

      <Section header="Recently added dates, series">
        <LibrarySelector
          label="Library Name"
          settingKey="settings-plex-series_library"
          settingKeyIds="settings-plex-series_library_ids"
          libraryType="show"
          description="Where to look the episode up when marking it recently added. Subtitle refreshes use the libraries on the Plex instance above."
        />
        <Check
          label="Mark episodes as recently added after downloading subtitles"
          settingKey="settings-plex-set_episode_added"
        />
      </Section>

      <Section header="Automation">
        <WebhookSelector
          label="Webhooks"
          description="Create a Bazarr webhook in Plex to automatically search for subtitles when content starts playing. Manage and remove existing webhooks for convenience."
        />
        <AutopulseSelector
          label="Autopulse Configuration"
          description={
            <>
              Generate a ready-to-use Autopulse configuration tailored to your
              Plex server. Includes optimized settings, OAuth authentication,
              and automatic path rewrite detection. Deploy as{" "}
              <Code>config.toml</Code> to your Autopulse data directory for a
              new setup, or copy specific sections to extend your existing
              configuration.
              <br />
              <br />
              To enable the webhook trigger, see the{" "}
              <Text
                component={Link}
                to="/settings/general"
                fw={500}
                c="blue"
                td="none"
              >
                Generic Webhook Configuration
              </Text>
              .
            </>
          }
        />
      </Section>
    </CollapseBox>
  );
};

export default PlexAccountSection;
