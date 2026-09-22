import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useSystemSettings } from "@/apis/hooks";
import { useOnboardingState } from "@/pages/Setup/useOnboardingState";
import Redirector from "./Redirector";

// Redirector only side-effects (navigates); we mock its inputs and assert where
// each kind of install is sent.
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
  Alert: ({ title, children }: { title: string; children: unknown }) => (
    <div role="alert">
      {title}
      {children as never}
    </div>
  ),
  Button: ({ children }: { children: unknown }) => (
    <button>{children as never}</button>
  ),
  Center: ({ children }: { children: unknown }) => (
    <div>{children as never}</div>
  ),
  Stack: ({ children }: { children: unknown }) => (
    <div>{children as never}</div>
  ),
}));

const mockedSettings = vi.mocked(useSystemSettings);
const mockedOnboarding = vi.mocked(useOnboardingState);

function withState({
  settings,
  needsOnboarding,
  isLoading,
  isError = false,
}: {
  settings: unknown;
  needsOnboarding: boolean;
  isLoading: boolean;
  isError?: boolean;
}) {
  mockedSettings.mockReturnValue({
    data: settings,
    isError,
    error: isError ? new Error("Network Error") : null,
    isFetching: false,
    refetch: vi.fn(),
  } as unknown as ReturnType<typeof useSystemSettings>);
  mockedOnboarding.mockReturnValue({ needsOnboarding, isLoading });
}

describe("Redirector", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("sends a fresh install to the first-run wizard", () => {
    // Nothing configured anywhere, so the wizard is what this install is for.
    // Discover still opens for every install that has something to show; this
    // is the one case that has nothing at all.
    withState({
      settings: { general: { use_sonarr: false, use_radarr: false } },
      needsOnboarding: true,
      isLoading: false,
    });

    render(<Redirector />);

    expect(navigate).toHaveBeenCalledWith("/setup", { replace: true });
    expect(navigate).not.toHaveBeenCalledWith("/discover", { replace: true });
  });

  it("opens Discover for an install that has been configured", () => {
    withState({
      settings: { general: { use_sonarr: true, use_radarr: false } },
      needsOnboarding: false,
      isLoading: false,
    });

    render(<Redirector />);

    expect(navigate).toHaveBeenCalledWith("/discover", { replace: true });
    expect(navigate).not.toHaveBeenCalledWith("/setup", { replace: true });
  });

  it("waits for both reads instead of deciding on one of them", () => {
    // needsOnboarding defaults to true while the reads are in flight, so
    // deciding early sends a configured install to the wizard. Deciding on
    // settings alone sends a fresh one past it.
    withState({
      settings: { general: { use_sonarr: false, use_radarr: false } },
      needsOnboarding: true,
      isLoading: true,
    });
    render(<Redirector />);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("says so instead of spinning for ever when settings will not answer", () => {
    // The landing page of a Bazarr+ that cannot answer used to be a spinner
    // with no message and nothing to press, which is the first thing a broken
    // install shows its owner.
    withState({
      settings: undefined,
      needsOnboarding: true,
      isLoading: false,
      isError: true,
    });

    render(<Redirector />);

    expect(screen.getByRole("alert")).toHaveTextContent(/did not answer/i);
    expect(
      screen.getByRole("button", { name: /try again/i }),
    ).toBeInTheDocument();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("does not redirect before settings have answered", () => {
    withState({ settings: undefined, needsOnboarding: true, isLoading: false });
    render(<Redirector />);
    expect(navigate).not.toHaveBeenCalled();
  });
});
