import { apikeyFingerprint } from "@/apis/hooks/jellyfin";

describe("apikeyFingerprint", () => {
  it("returns the empty string for missing input", () => {
    expect(apikeyFingerprint(undefined)).toBe("");
    expect(apikeyFingerprint("")).toBe("");
  });

  it("is deterministic for the same input", () => {
    expect(apikeyFingerprint("abc123")).toBe(apikeyFingerprint("abc123"));
  });

  it("changes when the apikey changes (even by one character)", () => {
    // The whole point is to invalidate the cache when the apikey rotates,
    // so different keys must produce different fingerprints. Different
    // accounts on the same Jellyfin server can see different libraries.
    const a = apikeyFingerprint("apikey-aaaaaaaaaaaaaaaa");
    const b = apikeyFingerprint("apikey-bbbbbbbbbbbbbbbb");
    const c = apikeyFingerprint("apikey-aaaaaaaaaaaaaaab"); // one char diff
    expect(a).not.toBe(b);
    expect(a).not.toBe(c);
    expect(b).not.toBe(c);
  });

  it("returns 8 hex characters (FNV-1a 32-bit)", () => {
    expect(apikeyFingerprint("anything")).toMatch(/^[0-9a-f]{8}$/);
  });

  it("never includes the apikey itself in the fingerprint", () => {
    // Sanity: the cache key must never expose the secret to React Query
    // devtools or other cache walkers.
    const secret = "super-secret-token-DO-NOT-LEAK";
    expect(apikeyFingerprint(secret)).not.toContain(secret);
    expect(apikeyFingerprint(secret)).not.toContain("super");
    expect(apikeyFingerprint(secret)).not.toContain("DO-NOT-LEAK");
  });
});
