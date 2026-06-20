import { describe, expect, it, vi } from "vitest";
import { base64ToFile, expandArchives, isArchiveFile } from "./archives";

function file(name: string, content = "x"): File {
  return new File([content], name);
}

describe("isArchiveFile", () => {
  it("detects supported archive extensions case-insensitively", () => {
    expect(isArchiveFile(file("pack.zip"))).toBe(true);
    expect(isArchiveFile(file("pack.RAR"))).toBe(true);
    expect(isArchiveFile(file("Season 1.7z"))).toBe(true);
  });

  it("rejects non-archives", () => {
    expect(isArchiveFile(file("movie.srt"))).toBe(false);
    expect(isArchiveFile(file("noext"))).toBe(false);
  });
});

describe("base64ToFile", () => {
  it("decodes base64 content into a File with the given name", async () => {
    const f = base64ToFile("movie.srt", btoa("hello world"));
    expect(f.name).toBe("movie.srt");
    expect(await f.text()).toBe("hello world");
  });
});

describe("expandArchives", () => {
  it("passes plain subtitle files through untouched", async () => {
    const plain = file("movie.srt", "sub");
    const extract = vi.fn();
    const { files, errors } = await expandArchives([plain], extract);
    expect(files).toEqual([plain]);
    expect(errors).toEqual([]);
    expect(extract).not.toHaveBeenCalled();
  });

  it("replaces an archive with its extracted subtitle files", async () => {
    const archive = file("pack.zip", "zipbytes");
    const extract = vi.fn().mockResolvedValue({
      count: 2,
      files: [
        { name: "a.srt", content: btoa("aaa") },
        { name: "b.ass", content: btoa("bbb") },
      ],
    });
    const { files, errors } = await expandArchives([archive], extract);
    expect(extract).toHaveBeenCalledWith(archive);
    expect(files.map((f) => f.name)).toEqual(["a.srt", "b.ass"]);
    expect(await files[0].text()).toBe("aaa");
    expect(errors).toEqual([]);
  });

  it("records the archive name when extraction fails, keeping other files", async () => {
    const archive = file("bad.zip", "x");
    const plain = file("ok.srt", "ok");
    const extract = vi.fn().mockRejectedValue(new Error("boom"));
    const { files, errors } = await expandArchives([archive, plain], extract);
    expect(files).toEqual([plain]);
    expect(errors).toEqual(["bad.zip"]);
  });
});
