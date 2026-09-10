import { render } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useSystemSettings } from "@/apis/hooks";
import Redirector from "./Redirector";

// Redirector only side-effects (navigates); we mock its inputs and assert the
// navigation target for a fresh install vs. an already-configured one.
const navigate = vi.fn();

vi.mock("react-router", () => ({
  useNavigate: () => navigate,
}));

vi.mock("@/apis/hooks", () => ({
  useSystemSettings: vi.fn(),
}));

vi.mock("@mantine/core", () => ({
  LoadingOverlay: () => null,
}));

const mockedSettings = vi.mocked(useSystemSettings);

describe("Redirector", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("opens Discover for a fresh install without library setup", () => {
    mockedSettings.mockReturnValue({
      // eslint-disable-next-line camelcase -- settings transport field names
      data: { general: { use_sonarr: false, use_radarr: false } },
    } as unknown as ReturnType<typeof useSystemSettings>);

    render(<Redirector />);

    expect(navigate).toHaveBeenCalledWith("/discover", { replace: true });
  });

  it("opens Discover for a configured install", () => {
    mockedSettings.mockReturnValue({
      // eslint-disable-next-line camelcase -- settings transport field names
      data: { general: { use_sonarr: true, use_radarr: false } },
    } as unknown as ReturnType<typeof useSystemSettings>);

    render(<Redirector />);

    expect(navigate).toHaveBeenCalledWith("/discover", { replace: true });
    expect(navigate).not.toHaveBeenCalledWith("/setup", { replace: true });
  });
  it("waits for authenticated settings without redirecting during loading or failure", () => {
    mockedSettings.mockReturnValue({ data: undefined } as ReturnType<
      typeof useSystemSettings
    >);
    render(<Redirector />);
    expect(navigate).not.toHaveBeenCalled();
  });
});
