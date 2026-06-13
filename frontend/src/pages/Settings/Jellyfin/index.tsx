import { FunctionComponent } from "react";
import { Layout } from "@/pages/Settings/components";
import JellyfinSection from "./JellyfinSection";

const SettingsJellyfinView: FunctionComponent = () => {
  return (
    <Layout name="Interface">
      <JellyfinSection />
    </Layout>
  );
};

export default SettingsJellyfinView;
