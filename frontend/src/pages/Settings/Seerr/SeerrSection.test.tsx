/* eslint-disable camelcase */

import { FunctionComponent, PropsWithChildren, ReactElement } from "react";
import { useForm } from "@mantine/form";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import {
  FormContext,
  type FormValues,
} from "@/pages/Settings/utilities/FormValues";
import { customRender, screen } from "@/tests";
import SeerrSection from "./SeerrSection";

vi.mock("@/apis/hooks/seerr", () => ({
  useSeerrTestConnectionMutation: () => ({
    mutate: (_p: unknown, opts: { onSuccess: (d: unknown) => void }) =>
      opts.onSuccess({
        success: true,
        version: "3.3.0",
        application_title: "Seerr",
        acting_user: {
          id: 1,
          display_name: "Owner",
          can_request_movie: true,
          can_request_tv: true,
          can_request_4k_movie: true,
          can_request_4k_tv: true,
        },
      }),
  }),
}));

// A Settings section normally renders inside the Connections page Layout,
// which provides the settings FormContext. Rendered bare, its inputs call
// useFormValues() with no context and throw. Wrap it in a FormContext the
// way the app mounts it - the same pattern settings.test.tsx's
// JellyfinWithForm and MediaServerSection.test.tsx use, since there is no
// shared render helper for an isolated settings section.
const SettingsFormHarness: FunctionComponent<PropsWithChildren> = ({
  children,
}) => {
  const form = useForm<FormValues>({
    initialValues: { settings: {}, hooks: {} },
  });
  return <FormContext.Provider value={form}>{children}</FormContext.Provider>;
};

function renderSettingsSection(ui: ReactElement) {
  return customRender(<SettingsFormHarness>{ui}</SettingsFormHarness>);
}

describe("SeerrSection", () => {
  it("tests the typed connection and reads out the acting user", async () => {
    renderSettingsSection(<SeerrSection />);
    const user = userEvent.setup();
    expect(
      screen.queryByRole("textbox", { name: "Seerr URL" }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("switch", { name: "Enabled" }));
    await user.type(screen.getByLabelText("Seerr URL"), "http://seerr:5055");
    await user.type(screen.getByLabelText("API key"), "synthetic-key");
    await user.click(screen.getByRole("button", { name: "Test" }));
    expect(await screen.findByText("Seerr 3.3.0 as Owner")).toBeInTheDocument();
    expect(
      screen.getByText(/Requested as the Seerr owner and approved immediately/),
    ).toBeInTheDocument();
  });

  // The states this integration reports, and the fact that it never approves
  // anything, are explained on the guide and nowhere in the form.
  it("points at the Seerr guide", async () => {
    renderSettingsSection(<SeerrSection />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("switch", { name: "Enabled" }));
    expect(
      screen.getByRole("link", { name: "Read the Seerr guide" }),
    ).toHaveAttribute(
      "href",
      "https://lavx.github.io/bazarr/guides/seerr.html",
    );
  });
});
