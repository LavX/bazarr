/* eslint-disable camelcase -- API fixtures retain transport field names. */
import { FunctionComponent, PropsWithChildren } from "react";
import { useForm } from "@mantine/form";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import { useSettingsMutation } from "@/apis/hooks/system";
import queryClient from "@/apis/queries";
import {
  FormContext,
  type FormValues,
} from "@/pages/Settings/utilities/FormValues";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import FlareSolverrIntegration, {
  flaresolverrSettingKey,
} from "./FlareSolverrIntegration";

const Harness: FunctionComponent<PropsWithChildren> = ({ children }) => {
  const form = useForm<FormValues>({
    initialValues: { settings: {}, hooks: {} },
  });
  return (
    <FormContext.Provider value={form}>
      {children}
      <output data-testid="staged">
        {JSON.stringify(form.values.settings)}
      </output>
    </FormContext.Provider>
  );
};

const sibling = "http://flaresolverr:8191";
const status = (overrides: Partial<System.FlareSolverrStatus> = {}) => ({
  data: {
    url: "",
    source: "none",
    detected: null,
    detecting: false,
    sibling_found: overrides.detected != null,
    ...overrides,
  },
});

beforeEach(() => {
  queryClient.clear();
});

describe("FlareSolverrIntegration", () => {
  it("says there is no per-provider opt-out", async () => {
    server.use(
      http.get("/api/providers/flaresolverr", () =>
        HttpResponse.json(status()),
      ),
    );
    customRender(
      <Harness>
        <FlareSolverrIntegration />
      </Harness>,
    );
    expect(
      await screen.findByText(/There is no per-provider opt-out/),
    ).toBeInTheDocument();
  });

  it("offers a detected sibling but does not adopt it", async () => {
    server.use(
      http.get("/api/providers/flaresolverr", () =>
        HttpResponse.json(status({ detected: sibling })),
      ),
    );
    customRender(
      <Harness>
        <FlareSolverrIntegration />
      </Harness>,
    );
    expect(
      await screen.findByRole("button", { name: `Use ${sibling}` }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("FlareSolverr address")).toHaveValue("");
    expect(screen.getByTestId("staged")).toHaveTextContent("{}");
  });

  it("fills the field only when the hint is accepted", async () => {
    server.use(
      http.get("/api/providers/flaresolverr", () =>
        HttpResponse.json(status({ detected: sibling })),
      ),
    );
    customRender(
      <Harness>
        <FlareSolverrIntegration />
      </Harness>,
    );
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: `Use ${sibling}` }),
    );
    await waitFor(() =>
      expect(screen.getByLabelText("FlareSolverr address")).toHaveValue(
        sibling,
      ),
    );
    expect(screen.getByTestId("staged")).toHaveTextContent(
      JSON.stringify({ [flaresolverrSettingKey]: sibling }),
    );
    expect(
      screen.queryByRole("button", { name: `Use ${sibling}` }),
    ).not.toBeInTheDocument();
  });

  it("detect reports what answered without writing the setting", async () => {
    server.use(
      http.get("/api/providers/flaresolverr", () =>
        HttpResponse.json(status()),
      ),
      http.post("/api/providers/flaresolverr", () =>
        HttpResponse.json(status({ detected: sibling })),
      ),
    );
    customRender(
      <Harness>
        <FlareSolverrIntegration />
      </Harness>,
    );
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Detect" }));
    expect(
      await screen.findByRole("button", { name: `Use ${sibling}` }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("staged")).toHaveTextContent("{}");
  });

  it("says so when nothing answers", async () => {
    server.use(
      http.get("/api/providers/flaresolverr", () =>
        HttpResponse.json(status()),
      ),
      http.post("/api/providers/flaresolverr", () =>
        HttpResponse.json(status()),
      ),
    );
    customRender(
      <Harness>
        <FlareSolverrIntegration />
      </Harness>,
    );
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Detect" }));
    expect(
      await screen.findByText(/Nothing answered at the usual sibling address/),
    ).toBeInTheDocument();
  });

  it("names the environment variable when it supplies the address", async () => {
    server.use(
      http.get("/api/providers/flaresolverr", () =>
        HttpResponse.json(status({ url: "http://env:8191", source: "env" })),
      ),
    );
    customRender(
      <Harness>
        <FlareSolverrIntegration />
      </Harness>,
    );
    expect(
      await screen.findByText(/FLARESOLVERR_URL environment variable/),
    ).toBeInTheDocument();
  });

  it("keeps asking while the server is still looking, then shows the hint", async () => {
    let reads = 0;
    server.use(
      http.get("/api/providers/flaresolverr", () => {
        reads += 1;
        return HttpResponse.json(
          reads === 1
            ? status({ detecting: true })
            : status({ detected: sibling }),
        );
      }),
    );
    customRender(
      <Harness>
        <FlareSolverrIntegration />
      </Harness>,
    );
    expect(
      await screen.findByText(/Looking for a FlareSolverr container/),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole(
        "button",
        { name: `Use ${sibling}` },
        { timeout: 5000 },
      ),
    ).toBeInTheDocument();
  });

  it("says no new address is offered when the sibling is the one in use", async () => {
    server.use(
      http.get("/api/providers/flaresolverr", () =>
        HttpResponse.json(status({ url: sibling, source: "setting" })),
      ),
      http.post("/api/providers/flaresolverr", () =>
        HttpResponse.json(
          status({ url: sibling, source: "setting", sibling_found: true }),
        ),
      ),
    );
    customRender(
      <Harness>
        <FlareSolverrIntegration />
      </Harness>,
    );
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Detect" }));
    expect(
      await screen.findByText(/No new address to offer/),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/Nothing answered at the usual sibling address/),
    ).not.toBeInTheDocument();
  });

  it("shows the fresh status after Detect and then a settings save", async () => {
    let saved = false;
    server.use(
      http.get("/api/providers/flaresolverr", () =>
        HttpResponse.json(
          saved
            ? status({ url: "http://typed:8191", source: "setting" })
            : status({ url: "http://env:8191", source: "env" }),
        ),
      ),
      http.post("/api/providers/flaresolverr", () =>
        HttpResponse.json(status({ url: "http://env:8191", source: "env" })),
      ),
      http.post("/api/system/settings", () => {
        saved = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const SaveButton: FunctionComponent = () => {
      const save = useSettingsMutation();
      return (
        <button
          type="button"
          onClick={() =>
            save.mutate({ [flaresolverrSettingKey]: "http://typed:8191" })
          }
        >
          Save now
        </button>
      );
    };
    customRender(
      <Harness>
        <FlareSolverrIntegration />
        <SaveButton />
      </Harness>,
    );
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Detect" }));
    expect(
      await screen.findByText(/FLARESOLVERR_URL environment variable/),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save now" }));
    await waitFor(() =>
      expect(
        screen.queryByText(/FLARESOLVERR_URL environment variable/),
      ).not.toBeInTheDocument(),
    );
  });
});
