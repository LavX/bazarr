/**
 * Tests for useAppTitle, the suffix shared by every document title.
 *
 * Titles used to end at the instance name alone, so a tab read "Discover -
 * Bazarr+" with no way to tell which build was running. The suffix now carries
 * the version, and the API reports it without its leading "v", so the hook has
 * to put that back.
 */

import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAppTitle } from "@/apis/hooks/site";

const systemSettings = vi.fn();
const systemStatus = vi.fn();

vi.mock("@/apis/hooks", () => ({
  useSystemSettings: () => systemSettings(),
  useSystemStatus: () => systemStatus(),
}));

const settings = (instanceName?: string) => ({
  data: instanceName ? { general: { instance_name: instanceName } } : undefined,
});

const status = (version?: string) => ({
  data: version ? { bazarr_version: version } : undefined,
});

describe("useAppTitle", () => {
  beforeEach(() => {
    systemSettings.mockReturnValue(settings("Bazarr+"));
    systemStatus.mockReturnValue(status("2.7.0"));
  });

  it("appends the version with a v prefix", () => {
    const { result } = renderHook(() => useAppTitle());

    expect(result.current).toBe("Bazarr+ v2.7.0");
  });

  it("keeps a version that already carries its prefix", () => {
    systemStatus.mockReturnValue(status("v2.7.0-alpha.1"));

    const { result } = renderHook(() => useAppTitle());

    expect(result.current).toBe("Bazarr+ v2.7.0-alpha.1");
  });

  it("keeps a custom instance name", () => {
    systemSettings.mockReturnValue(settings("Living Room"));

    const { result } = renderHook(() => useAppTitle());

    expect(result.current).toBe("Living Room v2.7.0");
  });

  it("falls back to the name alone when the version is unknown", () => {
    systemStatus.mockReturnValue(status("unknown"));

    const { result } = renderHook(() => useAppTitle());

    expect(result.current).toBe("Bazarr+");
  });

  it("uses the default name when the stored name is blank", () => {
    systemSettings.mockReturnValue({
      data: { general: { instance_name: "  " } },
    });

    const { result } = renderHook(() => useAppTitle());

    expect(result.current).toBe("Bazarr+ v2.7.0");
  });

  it("uses the default name while settings are still loading", () => {
    systemSettings.mockReturnValue(settings());
    systemStatus.mockReturnValue(status());

    const { result } = renderHook(() => useAppTitle());

    expect(result.current).toBe("Bazarr+");
  });
});
