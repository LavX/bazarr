import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  useArrInstances,
  useCreateArrInstance,
  useDeleteArrInstance,
  useSettingsMutation,
  useSystemSettings,
  useTestArrInstanceConnection,
  useUpdateArrInstance,
} from "@/apis/hooks";
import {
  readConnectionTests,
  recordConnectionTest,
} from "@/pages/Setup/connectionTests";
import { customRender, screen, waitFor } from "@/tests";
import ArrStep from "./ArrStep";

// Keep the real barrel (AllProviders' ThemeLoader reads useSystemSettings from
// it) and override only the hooks the step drives.
vi.mock("@/apis/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/apis/hooks")>();
  return {
    ...actual,
    useArrInstances: vi.fn(),
    useCreateArrInstance: vi.fn(),
    useDeleteArrInstance: vi.fn(),
    useTestArrInstanceConnection: vi.fn(),
    useSettingsMutation: vi.fn(),
    useSystemSettings: vi.fn(),
    useUpdateArrInstance: vi.fn(),
  };
});

const mockedUseArrInstances = vi.mocked(useArrInstances);
const mockedUseCreateArrInstance = vi.mocked(useCreateArrInstance);
const mockedUseDeleteArrInstance = vi.mocked(useDeleteArrInstance);
const mockedUseTestArrInstanceConnection = vi.mocked(
  useTestArrInstanceConnection,
);
const mockedUseSettingsMutation = vi.mocked(useSettingsMutation);
const mockedUseSystemSettings = vi.mocked(useSystemSettings);
const mockedUseUpdateArrInstance = vi.mocked(useUpdateArrInstance);

const onNext = vi.fn();
const createMutate = vi.fn();
const deleteMutate = vi.fn();
const testMutate = vi.fn();
const settingsMutate = vi.fn();
const updateMutate = vi.fn();

function setInstances(data: unknown) {
  mockedUseArrInstances.mockReturnValue({
    data,
  } as unknown as ReturnType<typeof useArrInstances>);
}

// The arr master switches always exist in the real settings. They default on
// here, and a test that is about a switched-off kind says so.
function setGeneral(general: Record<string, unknown>) {
  mockedUseSystemSettings.mockReturnValue({
    data: {
      general: {
        // eslint-disable-next-line camelcase
        use_sonarr: true,
        // eslint-disable-next-line camelcase
        use_radarr: true,
        // eslint-disable-next-line camelcase
        use_sportarr: true,
        ...general,
      },
    },
  } as unknown as ReturnType<typeof useSystemSettings>);
}

function setTestState(state: Record<string, unknown>) {
  mockedUseTestArrInstanceConnection.mockReturnValue({
    mutate: testMutate,
    reset: vi.fn(),
    isPending: false,
    isError: false,
    isSuccess: false,
    data: undefined,
    ...state,
  } as unknown as ReturnType<typeof useTestArrInstanceConnection>);
}

// The row the API hands back, as the create mutation reports it.
const created = (id: number) => ({
  id,
  kind: "sonarr",
  name: "Main Sonarr",
  ip: "10.0.0.5",
  port: 8989,
  base_url: "",
  ssl: false,
});

// Both writes succeed by default, so a test that cares about a failure sets
// only the one it is about.
function succeedingMutation(mutate: typeof createMutate) {
  mutate.mockImplementation(
    (_body: unknown, opts?: { onSuccess?: (data: unknown) => void }) => {
      opts?.onSuccess?.(created(1));
    },
  );
}

async function fillValidConnection(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText(/address/i), "10.0.0.5");
  await user.type(screen.getByLabelText(/api key/i), "abc123");
}

describe("ArrStep", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    setInstances([]);
    setTestState({});
    mockedUseCreateArrInstance.mockReturnValue({
      mutate: createMutate,
      isPending: false,
    } as unknown as ReturnType<typeof useCreateArrInstance>);
    mockedUseDeleteArrInstance.mockReturnValue({
      mutate: deleteMutate,
      isPending: false,
    } as unknown as ReturnType<typeof useDeleteArrInstance>);
    mockedUseSettingsMutation.mockReturnValue({
      mutate: settingsMutate,
      isPending: false,
    } as unknown as ReturnType<typeof useSettingsMutation>);
    mockedUseUpdateArrInstance.mockReturnValue({
      mutate: updateMutate,
      isPending: false,
    } as unknown as ReturnType<typeof useUpdateArrInstance>);
    setGeneral({});
    succeedingMutation(createMutate);
    succeedingMutation(settingsMutate);
    succeedingMutation(updateMutate);
  });

  it("renders bespoke connection fields for the kind", () => {
    customRender(<ArrStep kind="sonarr" onNext={onNext} />);

    expect(
      screen.getByRole("heading", { name: /sonarr/i }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText(/name/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/address/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/port/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/api key/i)).toBeInTheDocument();
  });

  // The defect this step shipped with: Continue on a form nobody filled in
  // posted it anyway, and the backend fills the blanks (empty key,
  // 127.0.0.1), so a new install came out of the wizard with up to three
  // enabled, default, unreachable instances scheduled for sync.
  it("writes nothing when the step was never filled in", async () => {
    const user = userEvent.setup();
    customRender(<ArrStep kind="sonarr" onNext={onNext} />);

    await user.click(
      screen.getByRole("button", { name: /continue without sonarr/i }),
    );

    expect(createMutate).not.toHaveBeenCalled();
    expect(settingsMutate).not.toHaveBeenCalled();
    expect(onNext).toHaveBeenCalled();
  });

  it("refuses to create until an address and an API key are given", async () => {
    const user = userEvent.setup();
    customRender(<ArrStep kind="sonarr" onNext={onNext} />);

    await user.type(screen.getByLabelText(/address/i), "10.0.0.5");
    await user.click(screen.getByRole("button", { name: /^continue$/i }));

    expect(createMutate).not.toHaveBeenCalled();
    expect(onNext).not.toHaveBeenCalled();
    expect(screen.getByText(/enter the sonarr api key/i)).toBeInTheDocument();

    await user.type(screen.getByLabelText(/api key/i), "abc123");
    await user.click(screen.getByRole("button", { name: /^continue$/i }));

    await waitFor(() => expect(createMutate).toHaveBeenCalled());
  });

  it("rejects a cleared port instead of posting zero", async () => {
    const user = userEvent.setup();
    customRender(<ArrStep kind="sonarr" onNext={onNext} />);

    await fillValidConnection(user);
    await user.clear(screen.getByLabelText(/port/i));
    await user.click(screen.getByRole("button", { name: /^continue$/i }));

    expect(createMutate).not.toHaveBeenCalled();
    expect(screen.getByText(/between 1 and 65535/i)).toBeInTheDocument();
  });

  it("keeps Test shut until there is a connection to test", async () => {
    const user = userEvent.setup();
    customRender(<ArrStep kind="sonarr" onNext={onNext} />);

    expect(screen.getByRole("button", { name: /test/i })).toBeDisabled();

    await user.type(screen.getByLabelText(/address/i), "10.0.0.5");
    expect(screen.getByRole("button", { name: /test/i })).toBeDisabled();

    await user.type(screen.getByLabelText(/api key/i), "abc123");
    expect(screen.getByRole("button", { name: /test/i })).toBeEnabled();
  });

  it("offers Sportarr with its own default port and use flag", async () => {
    const user = userEvent.setup();
    // The wizard connected only Sonarr and Radarr, so a Sportarr user had to
    // finish it and then find Settings > Connections.
    customRender(<ArrStep kind="sportarr" onNext={onNext} />);

    expect(
      screen.getByRole("heading", { name: /sportarr/i }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText(/port/i)).toHaveValue("1867");

    await fillValidConnection(user);
    await user.click(screen.getByRole("button", { name: /test/i }));
    expect(testMutate).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "sportarr", port: 1867 }),
    );

    await user.click(screen.getByRole("button", { name: /^continue$/i }));
    expect(createMutate).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "sportarr" }),
      expect.anything(),
    );
    expect(settingsMutate).toHaveBeenCalledWith(
      { "settings-general-use_sportarr": true },
      expect.anything(),
    );
  });

  it("tests the connection with the entered values and shows the result", async () => {
    const user = userEvent.setup();
    customRender(<ArrStep kind="sonarr" onNext={onNext} />);

    await fillValidConnection(user);
    await user.click(screen.getByRole("button", { name: /test/i }));

    expect(testMutate).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: "sonarr",
        ip: "10.0.0.5",
        api_key: "abc123",
        port: 8989,
      }),
    );

    // The verdict belongs to the connection it was measured against: the hook
    // is handed the current values so it can drop a green result the moment
    // the address, the port or the key changes.
    expect(mockedUseTestArrInstanceConnection).toHaveBeenLastCalledWith(
      expect.objectContaining({
        ip: "10.0.0.5",
        port: 8989,
        apiKey: "abc123",
      }),
    );

    // A successful result is surfaced inline.
    setTestState({
      data: { ok: true, app_name: "Sonarr", version: "4.0.0" },
      isSuccess: true,
    });
    customRender(<ArrStep kind="sonarr" onNext={onNext} />);
    expect(screen.getByText(/4\.0\.0/)).toBeInTheDocument();
  });

  it("creates the instance, enables use_sonarr, and advances on Continue", async () => {
    const user = userEvent.setup();
    customRender(<ArrStep kind="sonarr" onNext={onNext} />);

    await user.clear(screen.getByLabelText(/name/i));
    await user.type(screen.getByLabelText(/name/i), "Main Sonarr");
    await fillValidConnection(user);
    await user.click(screen.getByRole("button", { name: /^continue$/i }));

    await waitFor(() => {
      expect(createMutate).toHaveBeenCalledWith(
        expect.objectContaining({
          kind: "sonarr",
          name: "Main Sonarr",
          ip: "10.0.0.5",
          api_key: "abc123",
          port: 8989,
        }),
        expect.anything(),
      );
    });

    expect(settingsMutate).toHaveBeenCalledWith(
      { "settings-general-use_sonarr": true },
      expect.anything(),
    );
    expect(onNext).toHaveBeenCalled();
  });

  // Saving does not wait for a Test, so Finish has to be told whether one
  // passed. It used to call every saved row connected, a wrong key included.
  it("tells Finish a row saved without a Test was never tested", async () => {
    const user = userEvent.setup();
    customRender(<ArrStep kind="sonarr" onNext={onNext} />);

    await fillValidConnection(user);
    await user.click(screen.getByRole("button", { name: /^continue$/i }));

    await waitFor(() => expect(onNext).toHaveBeenCalled());
    expect(readConnectionTests()).toEqual({ "arr:1": "untested" });
  });

  it("tells Finish when the Test passed against the saved values", async () => {
    const user = userEvent.setup();
    setTestState({
      data: { ok: true, app_name: "Sonarr", version: "4.0.0" },
      isSuccess: true,
    });
    customRender(<ArrStep kind="sonarr" onNext={onNext} />);

    await fillValidConnection(user);
    await user.click(screen.getByRole("button", { name: /^continue$/i }));

    await waitFor(() => expect(onNext).toHaveBeenCalled());
    expect(readConnectionTests()).toEqual({ "arr:1": "passed" });
  });

  it("still saves after a failed Test, and tells Finish it failed", async () => {
    const user = userEvent.setup();
    setTestState({
      data: { ok: false, message: "Unauthorized" },
      isSuccess: true,
    });
    customRender(<ArrStep kind="sonarr" onNext={onNext} />);

    await fillValidConnection(user);
    await user.click(screen.getByRole("button", { name: /^continue$/i }));

    await waitFor(() => expect(createMutate).toHaveBeenCalled());
    await waitFor(() => expect(onNext).toHaveBeenCalled());
    expect(readConnectionTests()).toEqual({ "arr:1": "failed" });
  });

  // The wizard never asked which instance should be the default, so it must
  // not answer for the reader. The backend promotes the first enabled instance
  // of a kind on its own.
  it("does not claim the new row as the default", async () => {
    const user = userEvent.setup();
    customRender(<ArrStep kind="sonarr" onNext={onNext} />);

    await fillValidConnection(user);
    await user.click(screen.getByRole("button", { name: /^continue$/i }));

    await waitFor(() => expect(createMutate).toHaveBeenCalled());
    expect(createMutate.mock.calls[0][0]).not.toHaveProperty("is_default");
  });

  it("only flips use_sonarr once the create has succeeded", async () => {
    const user = userEvent.setup();
    // A create that never answers: nothing else may happen behind it.
    createMutate.mockImplementation(() => undefined);

    customRender(<ArrStep kind="sonarr" onNext={onNext} />);

    await fillValidConnection(user);
    await user.click(screen.getByRole("button", { name: /^continue$/i }));

    await waitFor(() => expect(createMutate).toHaveBeenCalled());
    expect(settingsMutate).not.toHaveBeenCalled();
    expect(onNext).not.toHaveBeenCalled();
  });

  it("surfaces a rejected create instead of advancing", async () => {
    const user = userEvent.setup();
    createMutate.mockImplementation(
      (_body: unknown, opts?: { onError?: (error: unknown) => void }) => {
        opts?.onError?.(new Error("nope"));
      },
    );

    customRender(<ArrStep kind="sonarr" onNext={onNext} />);

    await fillValidConnection(user);
    await user.click(screen.getByRole("button", { name: /^continue$/i }));

    expect(
      await screen.findByText(/could not save the sonarr connection/i),
    ).toBeInTheDocument();
    expect(onNext).not.toHaveBeenCalled();
  });

  it("says so when the instance saved but the switch did not", async () => {
    const user = userEvent.setup();
    settingsMutate.mockImplementation(
      (_body: unknown, opts?: { onError?: (error: unknown) => void }) => {
        opts?.onError?.(new Error("nope"));
      },
    );

    customRender(<ArrStep kind="sonarr" onNext={onNext} />);

    await fillValidConnection(user);
    await user.click(screen.getByRole("button", { name: /^continue$/i }));

    expect(
      await screen.findByText(/could not turn it on/i),
    ).toBeInTheDocument();
    expect(onNext).not.toHaveBeenCalled();
  });

  it("keeps the activation failure visible once the new row comes back", async () => {
    // The create invalidates the instances query, so the row this step just
    // wrote reappears while the step is still on screen and flips it into the
    // saved panel. That panel rendered no error at all, so the reader was told
    // the instance was connected and walked on with the kind still switched
    // off.
    const user = userEvent.setup();
    createMutate.mockImplementation(
      (_body: unknown, opts?: { onSuccess?: (data: unknown) => void }) => {
        setInstances([created(1)]);
        opts?.onSuccess?.(created(1));
      },
    );
    settingsMutate.mockImplementation(
      (_body: unknown, opts?: { onError?: (error: unknown) => void }) => {
        opts?.onError?.(new Error("nope"));
      },
    );

    customRender(<ArrStep kind="sonarr" onNext={onNext} />);

    await fillValidConnection(user);
    await user.click(screen.getByRole("button", { name: /^continue$/i }));

    expect(
      await screen.findByText("Saved, connection not tested"),
    ).toBeInTheDocument();
    expect(screen.getByText(/could not turn it on/i)).toBeInTheDocument();
    expect(onNext).not.toHaveBeenCalled();
  });

  it("never creates a second row when the switch failed and the list lags", async () => {
    // The create invalidates the instances query, so this step normally flips
    // into its connected state on its own. A refetch that is slow, or one that
    // fails outright, leaves it looking at an empty list with the instance
    // already written, and the second press posted the same Sonarr again:
    // two identical rows, each scheduled for sync, for one failed switch.
    const user = userEvent.setup();
    settingsMutate.mockImplementation(
      (_body: unknown, opts?: { onError?: (error: unknown) => void }) => {
        opts?.onError?.(new Error("nope"));
      },
    );

    customRender(<ArrStep kind="sonarr" onNext={onNext} />);

    await fillValidConnection(user);
    await user.click(screen.getByRole("button", { name: /^continue$/i }));
    expect(
      await screen.findByText(/could not turn it on/i),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^continue$/i }));

    expect(createMutate).toHaveBeenCalledTimes(1);
    // The press retries the part that failed instead.
    expect(settingsMutate).toHaveBeenCalledTimes(2);
    expect(onNext).not.toHaveBeenCalled();
  });

  it("shows the saved row for a pre-existing instance and does not create", async () => {
    const user = userEvent.setup();
    setInstances([
      {
        id: 1,
        kind: "sonarr",
        name: "Existing Sonarr",
        enabled: true,
        ip: "10.0.0.5",
        port: 8989,
        base_url: "/",
        ssl: false,
      },
    ]);

    customRender(<ArrStep kind="sonarr" onNext={onNext} />);

    expect(screen.getByText(/existing sonarr/i)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^continue$/i }));

    expect(createMutate).not.toHaveBeenCalled();
    // Already on, both the row and the kind, so there is nothing to write.
    expect(updateMutate).not.toHaveBeenCalled();
    expect(settingsMutate).not.toHaveBeenCalled();
    expect(onNext).toHaveBeenCalled();
  });

  // A rerun of setup lands here with the row a previous pass saved. When that
  // row, or the kind's master switch, had since been turned off, Continue
  // walked straight past it: the panel presented it as the connection, Finish
  // counted it, and Bazarr+ never synced it.
  describe("an existing row that is switched off", () => {
    const row = {
      id: 4,
      kind: "sonarr",
      name: "Main Sonarr",
      enabled: false,
      ip: "10.0.0.5",
      port: 8989,
      base_url: "/",
      ssl: false,
    };

    it("is turned back on, then the kind, before the step advances", async () => {
      const user = userEvent.setup();
      setInstances([row]);

      customRender(<ArrStep kind="sonarr" onNext={onNext} />);

      expect(
        screen.getByText(/continue turns it back on/i),
      ).toBeInTheDocument();

      await user.click(screen.getByRole("button", { name: /^continue$/i }));

      expect(updateMutate).toHaveBeenCalledWith(
        { id: 4, body: { enabled: true } },
        expect.anything(),
      );
      expect(settingsMutate).toHaveBeenCalledWith(
        { "settings-general-use_sonarr": true },
        expect.anything(),
      );
      expect(updateMutate.mock.invocationCallOrder[0]).toBeLessThan(
        settingsMutate.mock.invocationCallOrder[0],
      );
      expect(onNext).toHaveBeenCalled();
    });

    it("switches the kind back on when only the switch is off", async () => {
      const user = userEvent.setup();
      setInstances([{ ...row, enabled: true }]);
      // eslint-disable-next-line camelcase
      setGeneral({ use_sonarr: false });

      customRender(<ArrStep kind="sonarr" onNext={onNext} />);

      expect(
        screen.getByText(/continue turns it back on/i),
      ).toBeInTheDocument();

      await user.click(screen.getByRole("button", { name: /^continue$/i }));

      expect(updateMutate).not.toHaveBeenCalled();
      expect(settingsMutate).toHaveBeenCalledWith(
        { "settings-general-use_sonarr": true },
        expect.anything(),
      );
      expect(onNext).toHaveBeenCalled();
    });

    it("stays on the step when the row cannot be turned back on", async () => {
      const user = userEvent.setup();
      setInstances([row]);
      updateMutate.mockImplementation(
        (_body: unknown, opts?: { onError?: (error: unknown) => void }) => {
          opts?.onError?.(new Error("boom"));
        },
      );

      customRender(<ArrStep kind="sonarr" onNext={onNext} />);

      await user.click(screen.getByRole("button", { name: /^continue$/i }));

      expect(
        await screen.findByText(/could not turn main sonarr back on/i),
      ).toBeInTheDocument();
      expect(settingsMutate).not.toHaveBeenCalled();
      expect(onNext).not.toHaveBeenCalled();
    });

    it("is not the row shown while another of the kind is on", async () => {
      const user = userEvent.setup();
      setInstances([
        { ...row, id: 3, name: "Old Sonarr" },
        { ...row, name: "Main Sonarr", enabled: true },
      ]);

      customRender(<ArrStep kind="sonarr" onNext={onNext} />);

      expect(screen.getByText(/^main sonarr at/i)).toBeInTheDocument();
      expect(screen.queryByText(/old sonarr/i)).toBeNull();

      await user.click(screen.getByRole("button", { name: /^continue$/i }));

      expect(updateMutate).not.toHaveBeenCalled();
      expect(settingsMutate).not.toHaveBeenCalled();
      expect(onNext).toHaveBeenCalled();
    });
  });

  // Back lands on the saved panel, and it used to call every row "Already
  // connected" while Finish, a few screens on, said the Test failed or never
  // ran. Both read the same record now.
  describe("says what Finish will say about a saved row", () => {
    const existing = {
      id: 4,
      kind: "sonarr",
      name: "Main Sonarr",
      ip: "10.0.0.5",
      port: 8989,
      base_url: "/",
      ssl: false,
    };

    it("calls it connected only after a passing Test", () => {
      setInstances([existing]);
      recordConnectionTest("arr:4", "passed");

      customRender(<ArrStep kind="sonarr" onNext={onNext} />);

      expect(screen.getByText("Already connected")).toBeInTheDocument();
    });

    it("says the Test failed when it did", () => {
      setInstances([existing]);
      recordConnectionTest("arr:4", "failed");

      customRender(<ArrStep kind="sonarr" onNext={onNext} />);

      expect(
        screen.getByText("Saved, but the connection test failed"),
      ).toBeInTheDocument();
      expect(screen.queryByText(/already connected/i)).toBeNull();
    });

    it("says it was never tested when no Test ran", () => {
      setInstances([existing]);

      customRender(<ArrStep kind="sonarr" onNext={onNext} />);

      expect(
        screen.getByText("Saved, connection not tested"),
      ).toBeInTheDocument();
      expect(screen.queryByText(/already connected/i)).toBeNull();
    });

    it("reads the Test from the pass that saved the row", async () => {
      const user = userEvent.setup();
      setTestState({
        data: { ok: true, app_name: "Sonarr", version: "4.0.0" },
        isSuccess: true,
      });
      const view = customRender(<ArrStep kind="sonarr" onNext={onNext} />);

      await fillValidConnection(user);
      await user.click(screen.getByRole("button", { name: /^continue$/i }));
      await waitFor(() => expect(onNext).toHaveBeenCalled());
      view.unmount();

      // Going Back remounts the step with the new row in the list.
      setInstances([created(1)]);
      customRender(<ArrStep kind="sonarr" onNext={onNext} />);

      expect(screen.getByText("Already connected")).toBeInTheDocument();
    });
  });

  // Without this, a typo made on the previous pass was unfixable from the
  // wizard: Back showed a read-only panel with no edit and no delete.
  it("can remove a wrong instance from the connected panel", async () => {
    const user = userEvent.setup();
    setInstances([
      {
        id: 7,
        kind: "sonarr",
        name: "Typo Sonarr",
        ip: "10.0.0.9",
        port: 8989,
        base_url: "/",
        ssl: false,
      },
    ]);

    customRender(<ArrStep kind="sonarr" onNext={onNext} />);

    await user.click(
      screen.getByRole("button", { name: /remove and enter it again/i }),
    );
    // Destructive, so it asks once before it acts.
    expect(deleteMutate).not.toHaveBeenCalled();

    await user.click(
      screen.getByRole("button", { name: /remove typo sonarr/i }),
    );

    expect(deleteMutate).toHaveBeenCalledWith(7, expect.anything());
  });

  it("renders no skip control of its own", () => {
    // Skipping is the shell's job now, from step.optional, with one label for
    // every step. A second skip here is what made "Skip for now" and "Skip"
    // read as two different promises when they were the same call.
    customRender(<ArrStep kind="radarr" onNext={onNext} />);

    expect(
      screen.queryByRole("button", { name: /skip/i }),
    ).not.toBeInTheDocument();
  });
});
