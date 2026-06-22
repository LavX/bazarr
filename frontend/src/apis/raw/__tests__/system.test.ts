import { AxiosResponse } from "axios";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import client from "@/apis/raw/client";
import systemApi from "@/apis/raw/system";

// Helpers to extract FormData entries as a plain object for assertions.
function formDataToObject(fd: FormData): Record<string, string[]> {
  const result: Record<string, string[]> = {};
  fd.forEach((value, key) => {
    if (!(key in result)) {
      result[key] = [];
    }
    result[key].push(value as string);
  });
  return result;
}

// Minimal Axios-shaped response that satisfies the AxiosResponse<void> return type.
function okResponse(): AxiosResponse<void> {
  return {
    data: undefined,
    status: 200,
    statusText: "OK",
    headers: {},
    config: { headers: {} } as AxiosResponse["config"],
  };
}

describe("SystemApi.updateSettings", () => {
  let postSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    postSpy = vi.spyOn(client.axios, "post").mockResolvedValue(okResponse());
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("converts null values to the string 'null' so the backend disables the setting", async () => {
    await systemApi.updateSettings({ auth_type: null });

    expect(postSpy).toHaveBeenCalledOnce();
    const formData = postSpy.mock.calls[0][1] as FormData;
    const entries = formDataToObject(formData);

    expect(entries["auth_type"]).toEqual(["null"]);
  });

  it("omits undefined values entirely (leaves the field absent from the payload)", async () => {
    await systemApi.updateSettings({ opt_field: undefined });

    const formData = postSpy.mock.calls[0][1] as FormData;
    const entries = formDataToObject(formData);

    expect("opt_field" in entries).toBe(false);
  });

  it("passes string values through unchanged", async () => {
    await systemApi.updateSettings({ proxy_type: "socks5" });

    const formData = postSpy.mock.calls[0][1] as FormData;
    const entries = formDataToObject(formData);

    expect(entries["proxy_type"]).toEqual(["socks5"]);
  });

  it("passes numeric values through unchanged (coerced to string by FormData)", async () => {
    await systemApi.updateSettings({ port: 8080 });

    const formData = postSpy.mock.calls[0][1] as FormData;
    const entries = formDataToObject(formData);

    // FormData.append coerces numbers to strings.
    expect(entries["port"]).toEqual(["8080"]);
  });

  it("passes non-empty arrays as repeated form fields", async () => {
    await systemApi.updateSettings({ languages: ["en", "fr"] });

    const formData = postSpy.mock.calls[0][1] as FormData;
    const entries = formDataToObject(formData);

    expect(entries["languages"]).toEqual(["en", "fr"]);
  });

  it("represents an empty array as a single empty-string field", async () => {
    await systemApi.updateSettings({ languages: [] });

    const formData = postSpy.mock.calls[0][1] as FormData;
    const entries = formDataToObject(formData);

    expect(entries["languages"]).toEqual([""]);
  });

  it("sanitizes a mixed payload: null->string-null, undefined absent, others unchanged", async () => {
    await systemApi.updateSettings({
      auth_type: null,
      proxy_type: undefined,
      host: "192.168.1.1",
      port: 9090,
      tags: ["en", "de"],
      empty_tags: [],
    });

    expect(postSpy).toHaveBeenCalledOnce();
    const formData = postSpy.mock.calls[0][1] as FormData;
    const entries = formDataToObject(formData);

    // null field is sent as the literal string "null".
    expect(entries["auth_type"]).toEqual(["null"]);

    // undefined field must be absent entirely.
    expect("proxy_type" in entries).toBe(false);

    // Plain string passes through.
    expect(entries["host"]).toEqual(["192.168.1.1"]);

    // Number is coerced to string by FormData.
    expect(entries["port"]).toEqual(["9090"]);

    // Non-empty array becomes repeated fields.
    expect(entries["tags"]).toEqual(["en", "de"]);

    // Empty array becomes a single empty-string entry.
    expect(entries["empty_tags"]).toEqual([""]);
  });

  it("posts to the correct endpoint /api/system/settings", async () => {
    await systemApi.updateSettings({ host: "localhost" });

    const url = postSpy.mock.calls[0][0] as string;
    // The base prefix is /system and the client base URL is /api/, so the
    // composed path that reaches axios is "/system/settings".
    expect(url).toBe("/system/settings");
  });
});

describe("SystemApi.login", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("returns the response data on successful login", async () => {
    const payload = { upgrade_hash: false };
    vi.spyOn(client.axios, "post").mockResolvedValue({
      data: payload,
      status: 200,
      statusText: "OK",
      headers: {},
      config: { headers: {} } as AxiosResponse["config"],
    });

    const result = await systemApi.login("admin", "secret");

    expect(result).toEqual(payload);
  });

  it("sends credentials as form fields with action=login", async () => {
    const postSpy = vi
      .spyOn(client.axios, "post")
      .mockResolvedValue(okResponse());

    await systemApi.login("admin", "secret");

    expect(postSpy).toHaveBeenCalledOnce();
    const formData = postSpy.mock.calls[0][1] as FormData;
    const entries = formDataToObject(formData);

    expect(entries["username"]).toEqual(["admin"]);
    expect(entries["password"]).toEqual(["secret"]);

    // action is sent as a query param, not form body.
    const params = postSpy.mock.calls[0][2] as Record<string, unknown>;
    expect(params?.params).toEqual({ action: "login" });
  });
});
