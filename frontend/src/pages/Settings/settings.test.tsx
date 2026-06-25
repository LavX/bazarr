import { FunctionComponent } from "react";
import { useForm } from "@mantine/form";
import { http } from "msw";
import { HttpResponse } from "msw";
import server from "@/tests/mocks/node";
import { renderTest, RenderTestCase } from "@/tests/render";
import JellyfinSection from "./Jellyfin/JellyfinSection";
import { FormContext, type FormValues } from "./utilities/FormValues";
import SettingsGeneralView from "./General";
import SettingsLanguagesView from "./Languages";
import SettingsProvidersView from "./Providers";
import SettingsSchedulerView from "./Scheduler";
import SettingsSubtitlesView from "./Subtitles";
import SettingsUIView from "./UI";

// JellyfinSection normally renders inside the Connections page Layout, which
// provides the settings FormContext. Rendered bare, its inputs call
// useFormValues() with no context and throw (caught by the error boundary);
// under React 19 concurrent rendering that surfaced as a flaky test failure.
// Wrap it in a FormContext the way the app mounts it.
const JellyfinWithForm: FunctionComponent = () => {
  const form = useForm<FormValues>({
    initialValues: { settings: {}, hooks: {} },
  });
  return (
    <FormContext.Provider value={form}>
      <JellyfinSection />
    </FormContext.Provider>
  );
};

const cases: RenderTestCase[] = [
  {
    name: "general page",
    ui: SettingsGeneralView,
  },
  {
    name: "languages page",
    ui: SettingsLanguagesView,
    setupEach: () => {
      server.use(
        http.get("/api/system/languages", () => {
          return HttpResponse.json({});
        }),
      );
      server.use(
        http.get("/api/system/languages/profiles", () => {
          return HttpResponse.json({
            data: [],
          });
        }),
      );
      server.use(
        http.get("/api/system/status", () => {
          return HttpResponse.json({});
        }),
      );
    },
  },
  // TODO: Test Notifications Page
  {
    name: "providers page",
    ui: SettingsProvidersView,
    setupEach: () => {
      server.use(
        http.get("/api/provider-hub/catalog", () => {
          return HttpResponse.json({ sources: [], entries: [] });
        }),
      );
      server.use(
        http.get("/api/provider-hub/providers", () => {
          return HttpResponse.json({ data: [] });
        }),
      );
    },
  },
  // TODO: Test Radarr Page
  {
    name: "jellyfin page",
    ui: JellyfinWithForm,
  },
  {
    name: "scheduler page",
    ui: SettingsSchedulerView,
  },
  // TODO: Test Sonarr Page
  {
    name: "subtitles page",
    ui: SettingsSubtitlesView,
  },
  {
    name: "ui page",
    ui: SettingsUIView,
  },
];

renderTest("Settings", cases);
