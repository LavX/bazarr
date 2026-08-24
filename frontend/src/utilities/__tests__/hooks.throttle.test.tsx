/**
 * Regression test for a leaked timer in useThrottle.
 *
 * useDebouncedValue is built on useThrottle, which schedules a window.setTimeout
 * and previously never cleared it. A component that unmounted before the delay
 * elapsed left the timer scheduled, so the callback ran against a torn-down
 * environment. In the test suite that surfaced as an unhandled
 * "ReferenceError: window is not defined" attributed to whichever file happened
 * to be running, turning CI red while every test file passed. In the app it is a
 * state update on an unmounted component.
 */

import { useEffect } from "react";
import { render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useThrottle } from "@/utilities/hooks";

const DELAY = 500;

function Caller({ onFire }: { onFire: () => void }) {
  const throttled = useThrottle(onFire, DELAY);
  useEffect(() => {
    throttled();
  }, [throttled]);
  return null;
}

describe("useThrottle", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("does not fire after the component unmounts", () => {
    const onFire = vi.fn();
    const { unmount } = render(<Caller onFire={onFire} />);

    unmount();
    vi.advanceTimersByTime(DELAY * 2);

    expect(onFire).not.toHaveBeenCalled();
  });

  it("still fires while the component is mounted", () => {
    const onFire = vi.fn();
    render(<Caller onFire={onFire} />);

    vi.advanceTimersByTime(DELAY * 2);

    expect(onFire).toHaveBeenCalledTimes(1);
  });
});
