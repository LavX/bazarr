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

const sibling = "http://flaresolverr:8191/v1";
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

  describe("Test", () => {
    const result = (
      overrides: Partial<System.FlareSolverrTestResult> = {},
    ): { data: System.FlareSolverrTestResult } => ({
      data: {
        ok: true,
        code: "ok",
        message: "FlareSolverr 3.3.21 answered.",
        version: "3.3.21",
        source: "typed",
        ...overrides,
      },
    });

    const quietStatus = () =>
      http.get("/api/providers/flaresolverr", () =>
        HttpResponse.json(status()),
      );

    it("tests the address as typed, without saving it", async () => {
      let sent: string | null = null;
      server.use(
        quietStatus(),
        http.post("/api/providers/flaresolverr/test", async ({ request }) => {
          sent = (await request.formData()).get("url") as string;
          return HttpResponse.json(result());
        }),
      );
      customRender(
        <Harness>
          <FlareSolverrIntegration />
        </Harness>,
      );
      const user = userEvent.setup();
      await user.type(
        screen.getByLabelText("FlareSolverr address"),
        "http://solver.lan:8191/v1",
      );
      await user.click(screen.getByRole("button", { name: "Test" }));

      expect(
        await screen.findByText("FlareSolverr 3.3.21 answered."),
      ).toBeInTheDocument();
      expect(sent).toBe("http://solver.lan:8191/v1");
      expect(screen.getByTestId("staged")).toHaveTextContent(
        JSON.stringify({
          [flaresolverrSettingKey]: "http://solver.lan:8191/v1",
        }),
      );
    });

    it("announces a failure in a status region", async () => {
      server.use(
        quietStatus(),
        http.post("/api/providers/flaresolverr/test", () =>
          HttpResponse.json(
            result({
              ok: false,
              code: "unreachable",
              message:
                "Could not connect. Check the host name, the port and that FlareSolverr is running.",
              version: null,
            }),
          ),
        ),
      );
      customRender(
        <Harness>
          <FlareSolverrIntegration />
        </Harness>,
      );
      const user = userEvent.setup();
      await user.type(
        screen.getByLabelText("FlareSolverr address"),
        "http://solver.lan:8191",
      );
      await user.click(screen.getByRole("button", { name: "Test" }));

      const region = await screen.findByText(/Test failed\. Could not connect/);
      expect(region).toHaveAttribute("role", "status");
    });

    it("keeps the button busy while the test runs", async () => {
      let release: () => void = () => undefined;
      const gate = new Promise<void>((resolve) => {
        release = resolve;
      });
      server.use(
        quietStatus(),
        http.post("/api/providers/flaresolverr/test", async () => {
          await gate;
          return HttpResponse.json(result());
        }),
      );
      customRender(
        <Harness>
          <FlareSolverrIntegration />
        </Harness>,
      );
      const user = userEvent.setup();
      await user.type(
        screen.getByLabelText("FlareSolverr address"),
        "http://solver.lan:8191",
      );
      await user.click(screen.getByRole("button", { name: "Test" }));

      await waitFor(() =>
        expect(screen.getByRole("button", { name: "Test" })).toBeDisabled(),
      );
      expect(screen.getByText("Testing the address.")).toBeInTheDocument();
      release();
      expect(
        await screen.findByText("FlareSolverr 3.3.21 answered."),
      ).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Test" })).toBeEnabled();
    });

    it("can be reached and pressed from the keyboard", async () => {
      server.use(
        quietStatus(),
        http.post("/api/providers/flaresolverr/test", () =>
          HttpResponse.json(result()),
        ),
      );
      customRender(
        <Harness>
          <FlareSolverrIntegration />
        </Harness>,
      );
      const user = userEvent.setup();
      await user.type(
        screen.getByLabelText("FlareSolverr address"),
        "http://solver.lan:8191",
      );
      await user.tab();
      expect(screen.getByRole("button", { name: "Test" })).toHaveFocus();
      await user.keyboard("{Enter}");
      expect(
        await screen.findByText("FlareSolverr 3.3.21 answered."),
      ).toBeInTheDocument();
    });

    it("drops the result once the address changes", async () => {
      server.use(
        quietStatus(),
        http.post("/api/providers/flaresolverr/test", () =>
          HttpResponse.json(result()),
        ),
      );
      customRender(
        <Harness>
          <FlareSolverrIntegration />
        </Harness>,
      );
      const user = userEvent.setup();
      const field = screen.getByLabelText("FlareSolverr address");
      await user.type(field, "http://solver.lan:8191");
      await user.click(screen.getByRole("button", { name: "Test" }));
      await screen.findByText("FlareSolverr 3.3.21 answered.");

      await user.type(field, "0");

      await waitFor(() =>
        expect(
          screen.queryByText("FlareSolverr 3.3.21 answered."),
        ).not.toBeInTheDocument(),
      );
    });

    it("tests the environment address when the field is empty, and says so", async () => {
      let sent: string | null = null;
      server.use(
        http.get("/api/providers/flaresolverr", () =>
          HttpResponse.json(status({ url: "http://env:8191", source: "env" })),
        ),
        http.post("/api/providers/flaresolverr/test", async ({ request }) => {
          sent = (await request.formData()).get("url") as string;
          return HttpResponse.json(result({ source: "env" }));
        }),
      );
      customRender(
        <Harness>
          <FlareSolverrIntegration />
        </Harness>,
      );
      const user = userEvent.setup();
      const button = screen.getByRole("button", { name: "Test" });
      await waitFor(() => expect(button).toBeEnabled());
      await user.click(button);

      expect(
        await screen.findByText(
          "Tested the address from the FLARESOLVERR_URL environment variable. FlareSolverr 3.3.21 answered.",
        ),
      ).toBeInTheDocument();
      expect(sent).toBe("");
    });

    it("says it tested the saved address when the field is empty", async () => {
      server.use(
        http.get("/api/providers/flaresolverr", () =>
          HttpResponse.json(
            status({ url: "http://saved:8191", source: "setting" }),
          ),
        ),
        http.post("/api/providers/flaresolverr/test", () =>
          HttpResponse.json(
            result({
              ok: false,
              code: "timeout",
              message: "The address did not answer in time.",
              version: null,
              source: "setting",
            }),
          ),
        ),
      );
      customRender(
        <Harness>
          <FlareSolverrIntegration />
        </Harness>,
      );
      const user = userEvent.setup();
      const button = screen.getByRole("button", { name: "Test" });
      await waitFor(() => expect(button).toBeEnabled());
      await user.click(button);

      expect(
        await screen.findByText(
          "Test failed. Tested the saved address. The address did not answer in time.",
        ),
      ).toBeInTheDocument();
    });

    it("is disabled with a hint when there is nothing to test", async () => {
      server.use(quietStatus());
      customRender(
        <Harness>
          <FlareSolverrIntegration />
        </Harness>,
      );

      const button = screen.getByRole("button", { name: "Test" });
      const hint = await screen.findByText(
        "Type an address, or set FLARESOLVERR_URL, to test.",
      );
      // A plain hint: nothing announces it on page load, the status region is
      // for results, and the button points at it.
      for (const region of screen.getAllByRole("status")) {
        expect(region).not.toContainElement(hint);
      }
      expect(button).toBeDisabled();
      expect(button).toHaveAttribute("aria-describedby", hint.id);

      await userEvent
        .setup()
        .type(
          screen.getByLabelText("FlareSolverr address"),
          "http://solver.lan:8191",
        );
      await waitFor(() => expect(button).toBeEnabled());
      expect(
        screen.queryByText(
          "Type an address, or set FLARESOLVERR_URL, to test.",
        ),
      ).not.toBeInTheDocument();
    });

    it("stays enabled while the status is still loading", async () => {
      let release: () => void = () => undefined;
      const gate = new Promise<void>((resolve) => {
        release = resolve;
      });
      server.use(
        http.get("/api/providers/flaresolverr", async () => {
          await gate;
          return HttpResponse.json(status());
        }),
      );
      customRender(
        <Harness>
          <FlareSolverrIntegration />
        </Harness>,
      );

      expect(screen.getByRole("button", { name: "Test" })).toBeEnabled();
      expect(
        screen.queryByText(
          "Type an address, or set FLARESOLVERR_URL, to test.",
        ),
      ).not.toBeInTheDocument();
      release();
      expect(
        await screen.findByText(
          "Type an address, or set FLARESOLVERR_URL, to test.",
        ),
      ).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Test" })).toBeDisabled();
    });

    it("stays enabled when the status could not be read", async () => {
      let reads = 0;
      server.use(
        http.get("/api/providers/flaresolverr", () => {
          reads += 1;
          return HttpResponse.json({}, { status: 500 });
        }),
      );
      customRender(
        <Harness>
          <FlareSolverrIntegration />
        </Harness>,
      );

      await waitFor(() => expect(reads).toBeGreaterThan(0));
      await waitFor(() =>
        expect(screen.getByRole("button", { name: "Test" })).toBeEnabled(),
      );
      expect(
        screen.queryByText(
          "Type an address, or set FLARESOLVERR_URL, to test.",
        ),
      ).not.toBeInTheDocument();
    });

    it("drops the result when a save changes the address in use", async () => {
      let saved = false;
      server.use(
        http.get("/api/providers/flaresolverr", () =>
          HttpResponse.json(
            saved
              ? status()
              : status({ url: "http://env:8191", source: "env" }),
          ),
        ),
        http.post("/api/providers/flaresolverr/test", () =>
          HttpResponse.json(result({ source: "env" })),
        ),
        http.post("/api/system/settings", () => {
          saved = true;
          return new HttpResponse(null, { status: 204 });
        }),
      );
      const ClearButton: FunctionComponent = () => {
        const save = useSettingsMutation();
        return (
          <button
            type="button"
            onClick={() => save.mutate({ [flaresolverrSettingKey]: "" })}
          >
            Save now
          </button>
        );
      };
      customRender(
        <Harness>
          <FlareSolverrIntegration />
          <ClearButton />
        </Harness>,
      );
      const user = userEvent.setup();
      const button = screen.getByRole("button", { name: "Test" });
      await waitFor(() => expect(button).toBeEnabled());
      await user.click(button);
      await screen.findByText(/FlareSolverr 3\.3\.21 answered\./);

      await user.click(screen.getByRole("button", { name: "Save now" }));

      expect(
        await screen.findByText(
          "Type an address, or set FLARESOLVERR_URL, to test.",
        ),
      ).toBeInTheDocument();
      expect(
        screen.queryByText(/FlareSolverr 3\.3\.21 answered\./),
      ).not.toBeInTheDocument();
    });

    it("keeps a typed test that started before the status loaded", async () => {
      let releaseStatus: () => void = () => undefined;
      let releaseTest: () => void = () => undefined;
      const statusGate = new Promise<void>((resolve) => {
        releaseStatus = resolve;
      });
      const testGate = new Promise<void>((resolve) => {
        releaseTest = resolve;
      });
      let statusServed = false;
      server.use(
        http.get("/api/providers/flaresolverr", async () => {
          await statusGate;
          statusServed = true;
          return HttpResponse.json(
            status({ url: "http://env:8191", source: "env" }),
          );
        }),
        http.post("/api/providers/flaresolverr/test", async () => {
          await testGate;
          return HttpResponse.json(result());
        }),
      );
      customRender(
        <Harness>
          <FlareSolverrIntegration />
        </Harness>,
      );
      const user = userEvent.setup();
      await user.type(
        screen.getByLabelText("FlareSolverr address"),
        "http://solver.lan:8191",
      );
      await user.click(screen.getByRole("button", { name: "Test" }));
      expect(screen.getByText("Testing the address.")).toBeInTheDocument();

      releaseStatus();
      await waitFor(() => expect(statusServed).toBe(true));
      // Let the page take the answer in.
      await new Promise((resolve) => setTimeout(resolve, 100));
      expect(screen.getByText("Testing the address.")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Test" })).toBeDisabled();

      releaseTest();
      expect(
        await screen.findByText("FlareSolverr 3.3.21 answered."),
      ).toBeInTheDocument();
    });

    it("keeps a typed result when the address in use changes", async () => {
      let saved = false;
      let statusReads = 0;
      server.use(
        http.get("/api/providers/flaresolverr", () => {
          statusReads += 1;
          return HttpResponse.json(
            saved
              ? status({ url: "http://saved:8191", source: "setting" })
              : status({ url: "http://env:8191", source: "env" }),
          );
        }),
        http.post("/api/providers/flaresolverr/test", () =>
          HttpResponse.json(result()),
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
            onClick={() => save.mutate({ "settings-general-unrelated": "x" })}
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
      await waitFor(() => expect(statusReads).toBe(1));
      await user.type(
        screen.getByLabelText("FlareSolverr address"),
        "http://solver.lan:8191",
      );
      await user.click(screen.getByRole("button", { name: "Test" }));
      await screen.findByText("FlareSolverr 3.3.21 answered.");

      await user.click(screen.getByRole("button", { name: "Save now" }));
      // The status is read again and now names the saved address.
      await waitFor(() => expect(statusReads).toBeGreaterThan(1));
      await new Promise((resolve) => setTimeout(resolve, 100));

      expect(
        screen.getByText("FlareSolverr 3.3.21 answered."),
      ).toBeInTheDocument();
    });

    it("shows a failure in the error colour token", async () => {
      server.use(
        quietStatus(),
        http.post("/api/providers/flaresolverr/test", () =>
          HttpResponse.json(
            result({
              ok: false,
              code: "tls",
              message: "The secure connection failed.",
              version: null,
            }),
          ),
        ),
      );
      customRender(
        <Harness>
          <FlareSolverrIntegration />
        </Harness>,
      );
      const user = userEvent.setup();
      await user.type(
        screen.getByLabelText("FlareSolverr address"),
        "https://solver.lan:8191",
      );
      await user.click(screen.getByRole("button", { name: "Test" }));

      const region = await screen.findByText(/Test failed\. The secure/);
      expect(region.getAttribute("style") ?? "").toContain(
        "var(--mantine-color-error)",
      );
    });

    it("says so when the test itself could not run", async () => {
      server.use(
        quietStatus(),
        http.post("/api/providers/flaresolverr/test", () =>
          HttpResponse.json({}, { status: 500 }),
        ),
      );
      customRender(
        <Harness>
          <FlareSolverrIntegration />
        </Harness>,
      );
      const user = userEvent.setup();
      await user.type(
        screen.getByLabelText("FlareSolverr address"),
        "http://solver.lan:8191",
      );
      await user.click(screen.getByRole("button", { name: "Test" }));

      expect(
        await screen.findByText("The test could not be completed."),
      ).toBeInTheDocument();
    });
  });
});
