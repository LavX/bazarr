/**
 * The install stage's progress is driven by the standard jobs.
 *
 * Each install is a backend job; the stage counts a provider as settled when
 * the jobs socket reports its job completed or failed in the [System, Jobs]
 * cache, and a failed job's reason is what the outcome list shows. The hooks
 * that read the catalog and settings are stubbed; the install hook is real.
 */

import { act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  useProviderHubCatalog,
  useSettingsMutation,
  useSystem,
  useSystemSettings,
} from "@/apis/hooks";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import { customRender, screen, waitFor } from "@/tests";
import ProviderInstallStage from "./ProviderInstallStage";

vi.mock("@/apis/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/apis/hooks")>();
  return {
    ...actual,
    useProviderHubCatalog: vi.fn(),
    useSettingsMutation: vi.fn(),
    useSystem: vi.fn(),
    useSystemSettings: vi.fn(),
  };
});

vi.mock("@/apis/raw", () => ({
  default: {
    providerHub: { install: vi.fn(), refreshCatalog: vi.fn() },
    system: { jobs: vi.fn(), status: vi.fn() },
  },
}));

vi.mock("./redirect", () => ({ redirectToSetup: vi.fn() }));

const JOBS_KEY = [QueryKeys.System, QueryKeys.Jobs];
const restart = vi.fn();

function entry(providerId: string, name: string) {
  return {
    // eslint-disable-next-line camelcase
    provider_id: providerId,
    name,
    version: "1.0.0",
    trusted: true,
    manifest: { id: providerId, name },
  };
}

function report(jobs: Array<[number, string, string?]>) {
  act(() => {
    queryClient.setQueryData(
      JOBS_KEY,
      jobs.map(([id, status, message]) => ({
        /* eslint-disable camelcase */
        job_id: id,
        job_name: `Installing provider ${id}`,
        status,
        error:
          status === "failed"
            ? { reason: "failed", message: message ?? "" }
            : null,
        /* eslint-enable camelcase */
      })),
    );
  });
}

describe("ProviderInstallStage with standard jobs", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    queryClient.clear();
    vi.mocked(useProviderHubCatalog).mockReturnValue({
      data: {
        sources: [],
        entries: [
          entry("opensubtitles", "OpenSubtitles"),
          entry("subscene", "Subscene"),
        ],
      },
      isPending: false,
      isError: false,
      error: null,
      isFetching: false,
      refetch: vi.fn(),
    } as unknown as ReturnType<typeof useProviderHubCatalog>);
    vi.mocked(useSystemSettings).mockReturnValue({
      // eslint-disable-next-line camelcase
      data: { general: { enabled_providers: [] } },
    } as unknown as ReturnType<typeof useSystemSettings>);
    vi.mocked(useSettingsMutation).mockReturnValue({
      mutateAsync: vi.fn().mockResolvedValue(undefined),
      isPending: false,
    } as unknown as ReturnType<typeof useSettingsMutation>);
    vi.mocked(useSystem).mockReturnValue({
      restart,
      isMutating: false,
    } as unknown as ReturnType<typeof useSystem>);
    vi.mocked(api.system.jobs).mockResolvedValue([]);
    vi.mocked(api.system.status).mockReturnValue(new Promise(() => undefined));
    vi.mocked(api.providerHub.install).mockImplementation(
      async (manifest: LooseObject) => ({
        // eslint-disable-next-line camelcase
        job_id: manifest.id === "opensubtitles" ? 31 : 32,
      }),
    );
  });

  it("moves the bar as each install job settles and shows a failed job's reason", async () => {
    const user = userEvent.setup();
    customRender(
      <ProviderInstallStage
        hasInstalled={false}
        onInstalledNeedsRestart={vi.fn()}
        onUseInstalled={vi.fn()}
        onNext={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("checkbox", { name: /opensubtitles/i }));
    await user.click(screen.getByRole("checkbox", { name: /subscene/i }));
    await user.click(
      screen.getByRole("button", { name: /install & restart/i }),
    );

    await waitFor(() =>
      expect(api.providerHub.install).toHaveBeenCalledTimes(2),
    );
    // Queued is not finished: the requests answered, the jobs have not.
    expect(screen.getByText("0 of 2 done")).toBeInTheDocument();

    report([
      [31, "completed"],
      [32, "running"],
    ]);
    expect(await screen.findByText("1 of 2 done")).toBeInTheDocument();
    expect(restart).not.toHaveBeenCalled();

    report([
      [31, "completed"],
      [32, "failed", "Could not install Subscene: bundle hash mismatch"],
    ]);
    expect(
      await screen.findByText(
        "Could not install Subscene: bundle hash mismatch",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Installed, waiting for the restart"),
    ).toBeVisible();
  });

  it("restarts once every install job has completed", async () => {
    const user = userEvent.setup();
    const onInstalledNeedsRestart = vi.fn();
    customRender(
      <ProviderInstallStage
        hasInstalled={false}
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={vi.fn()}
        onNext={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("checkbox", { name: /opensubtitles/i }));
    await user.click(
      screen.getByRole("button", { name: /install & restart/i }),
    );
    await waitFor(() => expect(api.providerHub.install).toHaveBeenCalled());
    expect(restart).not.toHaveBeenCalled();

    report([[31, "completed"]]);
    await waitFor(() => expect(restart).toHaveBeenCalledTimes(1));
    expect(onInstalledNeedsRestart).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("Restarting Bazarr+")).toBeInTheDocument();
  });
});
