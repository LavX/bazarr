import { describe, expect, it } from "vitest";
import { syncStartedNotificationMessage } from "./SyncSubtitleForm";

describe("SyncSubtitleForm notifications", () => {
  it("reports sync job start instead of completed sync", () => {
    expect(syncStartedNotificationMessage(1)).toBe(
      "1 subtitle sync job(s) started. Track progress in Jobs Manager.",
    );
  });
});
