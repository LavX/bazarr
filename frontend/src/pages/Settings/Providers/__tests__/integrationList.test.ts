import { describe, expect, it } from "vitest";
import { IntegrationList } from "@/pages/Settings/Providers/list";

describe("IntegrationList OMDB entry", () => {
  it("offers OMDB as a selectable integration wired to settings-omdb-apikey", () => {
    const omdb = IntegrationList.find((p) => p.key === "omdb");
    expect(omdb).toBeDefined();
    // input key 'apikey' + provider key 'omdb' => settings-omdb-apikey,
    // the existing backend setting the compat endpoint reads.
    const apikey = omdb?.inputs?.find((i) => i.key === "apikey");
    expect(apikey).toBeDefined();
    expect(apikey?.type).toBe("password");
  });
});
