import { FunctionComponent, PropsWithChildren } from "react";
import { useForm } from "@mantine/form";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
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
  data: { url: "", source: "none", detected: null, ...overrides },
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
});
