import { render } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useSystemSettings } from "@/apis/hooks";
import { useOnboardingState } from "@/pages/Setup/useOnboardingState";
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

vi.mock("@/pages/Setup/useOnboardingState", () => ({
  useOnboardingState: vi.fn(),
}));

vi.mock("@mantine/core", () => ({
  LoadingOverlay: () => null,
}));

const mockedSettings = vi.mocked(useSystemSettings);
const mockedOnboarding = vi.mocked(useOnboardingState);

describe("Redirector", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("redirects a fresh install to /setup", () => {
    mockedOnboarding.mockReturnValue({
      needsOnboarding: true,
      isLoading: false,
    });
    mockedSettings.mockReturnValue({
      data: { general: { use_sonarr: false, use_radarr: false } },
    } as unknown as ReturnType<typeof useSystemSettings>);

    render(<Redirector />);

    expect(navigate).toHaveBeenCalledWith("/setup", { replace: true });
  });

  it("redirects a configured install to /series as before", () => {
    mockedOnboarding.mockReturnValue({
      needsOnboarding: false,
      isLoading: false,
    });
    mockedSettings.mockReturnValue({
      data: { general: { use_sonarr: true, use_radarr: false } },
    } as unknown as ReturnType<typeof useSystemSettings>);

    render(<Redirector />);

    expect(navigate).toHaveBeenCalledWith("/series", { replace: true });
    expect(navigate).not.toHaveBeenCalledWith("/setup", { replace: true });
  });
});
