import { FunctionComponent } from "react";
import {
  Check,
  Layout,
  Message,
  Section,
  Selector,
} from "@/pages/Settings/components";
import { uiPageSizeKey } from "@/utilities/storage";
import { colorSchemeOptions, pageSizeOptions } from "./options";

const SettingsUIView: FunctionComponent = () => {
  return (
    <Layout name="Interface">
      <Section header="List View">
        <Selector
          label="Page Size"
          options={pageSizeOptions}
          settingKey={uiPageSizeKey}
          defaultValue={50}
        ></Selector>
      </Section>
      <Section header="Style">
        <Selector
          label="Theme"
          options={colorSchemeOptions}
          settingKey="settings-general-theme"
          defaultValue={"auto"}
        ></Selector>
      </Section>
      <Section header="Badge Display">
        <Check
          label="Show Live Badge"
          settingKey="settings-general-show_live_badge"
        ></Check>
        <Message>
          Controls whether the "LIVE" badge appears when SignalR is connected.
          Turn this off if you prefer a cleaner interface. The "DOWN" badge will
          always appear when SignalR is disconnected, regardless of this
          setting. This option affects badge visibility only, not the actual
          connection status.
        </Message>
      </Section>
    </Layout>
  );
};

export default SettingsUIView;
