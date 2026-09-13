/* eslint-disable camelcase -- the backend browse contract spells instance_id */
import { AxiosResponse } from "axios";
import { afterEach, describe, expect, it, vi } from "vitest";
import client from "@/apis/raw/client";
import filesApi from "@/apis/raw/files";

function okResponse(): AxiosResponse<FileTree[]> {
  return {
    data: [],
    status: 200,
    statusText: "OK",
    headers: {},
    config: { headers: {} } as AxiosResponse["config"],
  };
}

describe("FilesApi.sportarr", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("browses the Sportarr side at the owning instance", async () => {
    const getSpy = vi
      .spyOn(client.axios, "get")
      .mockResolvedValue(okResponse());

    await filesApi.sportarr("/sports", 4);

    const [url, options] = getSpy.mock.calls[0];
    expect(url).toBe("/files/sportarr");
    expect(options?.params).toEqual({ path: "/sports", instance_id: 4 });
  });

  it("leaves the instance unset for the default server", async () => {
    const getSpy = vi
      .spyOn(client.axios, "get")
      .mockResolvedValue(okResponse());

    await filesApi.sportarr();

    const [url, options] = getSpy.mock.calls[0];
    expect(url).toBe("/files/sportarr");
    expect(options?.params).toEqual({
      path: undefined,
      instance_id: undefined,
    });
  });
});
