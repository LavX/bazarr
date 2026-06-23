import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useArrInstances } from "@/apis/hooks/arrInstances";
import { useSystemSettings } from "@/apis/hooks/system";
import type { ArrInstance } from "@/apis/raw/arrInstances";
import { useOnboardingState } from "./useOnboardingState";

// The hook is a pure derivation over two queries, so we mock the data hooks
// directly and assert the boolean it computes for each fresh/configured shape.
vi.mock("@/apis/hooks/system", () => ({
  useSystemSettings: vi.fn(),
}));

vi.mock("@/apis/hooks/arrInstances", () => ({
  useArrInstances: vi.fn(),
}));

const mockedSettings = vi.mocked(useSystemSettings);
const mockedInstances = vi.mocked(useArrInstances);

// A fresh install: nothing configured, no instances, setup not complete.
function freshSettings(overrides: Partial<Settings.General> = {}) {
  return {
    data: {
      general: {
        use_sonarr: false,
        use_radarr: false,
        enabled_providers: [],
        setup_complete: false,
        ...overrides,
      },
    },
    isLoading: false,
  } as unknown as ReturnType<typeof useSystemSettings>;
}

function instances(data: ArrInstance[] | undefined, isLoading = false) {
  return { data, isLoading } as unknown as ReturnType<typeof useArrInstances>;
}

describe("useOnboardingState", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("returns needsOnboarding=true for a fresh install", () => {
    mockedSettings.mockReturnValue(freshSettings());
    mockedInstances.mockReturnValue(instances([]));

    const { result } = renderHook(() => useOnboardingState());

    expect(result.current.needsOnboarding).toBe(true);
    expect(result.current.isLoading).toBe(false);
  });

  it("returns needsOnboarding=false when a sonarr instance exists", () => {
    mockedSettings.mockReturnValue(freshSettings());
    mockedInstances.mockReturnValue(
      instances([{ id: 1, kind: "sonarr" } as unknown as ArrInstance]),
    );

    const { result } = renderHook(() => useOnboardingState());

    expect(result.current.needsOnboarding).toBe(false);
  });

  it("returns needsOnboarding=false when use_sonarr is true", () => {
    mockedSettings.mockReturnValue(freshSettings({ use_sonarr: true }));
    mockedInstances.mockReturnValue(instances([]));

    const { result } = renderHook(() => useOnboardingState());

    expect(result.current.needsOnboarding).toBe(false);
  });

  it("returns needsOnboarding=false when use_radarr is true", () => {
    mockedSettings.mockReturnValue(freshSettings({ use_radarr: true }));
    mockedInstances.mockReturnValue(instances([]));

    const { result } = renderHook(() => useOnboardingState());

    expect(result.current.needsOnboarding).toBe(false);
  });

  it("returns needsOnboarding=false when a provider is enabled", () => {
    mockedSettings.mockReturnValue(
      freshSettings({ enabled_providers: ["opensubtitles"] }),
    );
    mockedInstances.mockReturnValue(instances([]));

    const { result } = renderHook(() => useOnboardingState());

    expect(result.current.needsOnboarding).toBe(false);
  });

  it("returns needsOnboarding=false when setup_complete is true", () => {
    mockedSettings.mockReturnValue(freshSettings({ setup_complete: true }));
    mockedInstances.mockReturnValue(instances([]));

    const { result } = renderHook(() => useOnboardingState());

    expect(result.current.needsOnboarding).toBe(false);
  });

  it("passes through isLoading when either query is loading", () => {
    mockedSettings.mockReturnValue({
      data: undefined,
      isLoading: true,
    } as unknown as ReturnType<typeof useSystemSettings>);
    mockedInstances.mockReturnValue(instances(undefined, false));

    const { result } = renderHook(() => useOnboardingState());

    expect(result.current.isLoading).toBe(true);
  });
});
