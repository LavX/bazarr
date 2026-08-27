import { describe, expect, it } from "vitest";
import { nextProfileIdFrom } from "@/pages/Settings/Languages/table";

const profile = (profileId: number) =>
  ({ profileId }) as unknown as Language.Profile;

describe("nextProfileIdFrom", () => {
  it("allocates past the staged profiles", () => {
    expect(nextProfileIdFrom([profile(1), profile(3)], [])).toBe(4);
  });

  it("never reuses the id of a profile deleted in the same staging session", () => {
    // Server knows 1..3; the user deleted 3 and is adding a new profile
    // before Apply. Reusing 3 would make the server treat it as an update
    // and silently repoint defaults that referenced the deleted profile.
    const staged = [profile(1), profile(2)];
    const original = [profile(1), profile(2), profile(3)];
    expect(nextProfileIdFrom(staged, original)).toBe(4);
  });

  it("starts at 1 with no profiles anywhere", () => {
    expect(nextProfileIdFrom([], [])).toBe(1);
  });
});
