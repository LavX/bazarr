import { FunctionComponent } from "react";
import { Check, Layout, Message, Section } from "@/pages/Settings/components";
import { NotificationView } from "./components";

const SettingsNotificationsView: FunctionComponent = () => {
  return (
    <Layout name="Notifications">
      <Section header="Notifications">
        <NotificationView></NotificationView>
      </Section>
      <Section header="Options">
        <Check
          label="Silent for Manual Actions"
          settingKey="settings-general-dont_notify_manual_actions"
        ></Check>
        <Message>
          Suppress notifications when manually download/upload subtitles.
        </Message>
        <Check
          label="Notify when a library sync finds no missing subtitles"
          settingKey="settings-general-notify_if_nothing_is_missing_for_signalr_event"
        ></Check>
        <Message>
          Send a notification when Sonarr or Radarr triggers a sync for an item
          that is already fully subtitled. Off by default.
        </Message>
      </Section>
    </Layout>
  );
};

export default SettingsNotificationsView;
