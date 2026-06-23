import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { useWizardStep } from "./useWizardStep";

const STORAGE_KEY = "bazarr.onboarding.step";

describe("useWizardStep", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("defaults to step 0", () => {
    const { result } = renderHook(() => useWizardStep());
    expect(result.current.step).toBe(0);
  });

  it("setStep updates the step and persists to localStorage", () => {
    const { result } = renderHook(() => useWizardStep());

    act(() => result.current.setStep(2));

    expect(result.current.step).toBe(2);
    expect(localStorage.getItem(STORAGE_KEY)).toBe("2");
  });

  it("next advances and persists", () => {
    const { result } = renderHook(() => useWizardStep());

    act(() => result.current.next());

    expect(result.current.step).toBe(1);
    expect(localStorage.getItem(STORAGE_KEY)).toBe("1");
  });

  it("back goes back one step but never below zero", () => {
    const { result } = renderHook(() => useWizardStep());

    act(() => result.current.setStep(2));
    act(() => result.current.back());
    expect(result.current.step).toBe(1);

    act(() => result.current.setStep(0));
    act(() => result.current.back());
    expect(result.current.step).toBe(0);
  });

  it("reset clears the persisted step", () => {
    const { result } = renderHook(() => useWizardStep());

    act(() => result.current.setStep(3));
    expect(localStorage.getItem(STORAGE_KEY)).toBe("3");

    act(() => result.current.reset());

    expect(result.current.step).toBe(0);
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("rehydrates the step from localStorage on mount", () => {
    localStorage.setItem(STORAGE_KEY, "4");

    const { result } = renderHook(() => useWizardStep());

    expect(result.current.step).toBe(4);
  });
});
