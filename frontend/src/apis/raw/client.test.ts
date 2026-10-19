import { afterEach, describe, expect, it, vi } from "vitest";
import client from "./client";
import { ownWriteErrors } from "./inlineWrites";

const showNotification = vi.fn();

vi.mock("@mantine/notifications", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@mantine/notifications")>()),
  showNotification: (args: unknown) => showNotification(args),
}));

// The interceptor's rejection path, driven the way axios drives it.
function fail(method: string) {
  const rejected = (
    client.axios.interceptors.response as unknown as {
      handlers: { rejected: (error: unknown) => Promise<unknown> }[];
    }
  ).handlers[0].rejected;
  return rejected({
    config: { method },
    response: { status: 400, data: { message: "Bad value" } },
    message: "Request failed",
  }).catch(() => undefined);
}

describe("inline write errors", () => {
  afterEach(() => showNotification.mockClear());

  it("toasts a failed write when no surface owns it", async () => {
    await fail("post");
    expect(showNotification).toHaveBeenCalledTimes(1);
  });

  it("skips the toast for a write while a surface owns write errors", async () => {
    const release = ownWriteErrors();
    await fail("post");
    await fail("patch");
    await fail("delete");
    expect(showNotification).not.toHaveBeenCalled();
    release();
    // Released twice is still released once.
    release();
    await fail("post");
    expect(showNotification).toHaveBeenCalledTimes(1);
  });

  it("still toasts a failed read while writes are owned", async () => {
    const release = ownWriteErrors();
    await fail("get");
    release();
    expect(showNotification).toHaveBeenCalledTimes(1);
  });
});
