import { PropsWithChildren } from "react";
import { showNotification } from "@mantine/notifications";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useRestoreBackups } from "@/apis/hooks/system";
import { QueryKeys } from "@/apis/queries/keys";
import { notification } from "@/modules/task";

const restoreBackups = vi.hoisted(() =>
  vi.fn().mockResolvedValue({
    restart: true,
    message: "Restore staged; Bazarr will restart to apply it",
  }),
);

vi.mock("@/apis/raw", () => ({
  default: {
    system: {
      restoreBackups,
    },
  },
}));

vi.mock("@mantine/notifications", () => ({
  showNotification: vi.fn(),
}));

function makeClientAndWrapper() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { networkMode: "offlineFirst" },
    },
  });
  const wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const spy = vi.spyOn(client, "invalidateQueries");
  return { wrapper, spy };
}

describe("useRestoreBackups", () => {
  beforeEach(() => {
    vi.mocked(showNotification).mockClear();
    restoreBackups.mockClear();
  });

  it("shows the staged-restore restart message as success", async () => {
    const { wrapper, spy } = makeClientAndWrapper();
    const { result } = renderHook(() => useRestoreBackups(), { wrapper });

    result.current.mutate("bazarr_backup_v1.2.3_2026.09.16_12.00.00.zip");

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(showNotification).toHaveBeenCalledWith(
      notification.info(
        "Backup restored",
        "Restore staged; Bazarr will restart to apply it",
      ),
    );
    expect(spy).toHaveBeenCalledWith({
      queryKey: [QueryKeys.System, QueryKeys.Backups],
    });
  });
});
