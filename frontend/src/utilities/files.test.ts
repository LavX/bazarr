import { filenameFromContentDisposition } from "./files";

describe("filenameFromContentDisposition", () => {
  it("parses the plain quoted form", () => {
    expect(
      filenameFromContentDisposition(
        'attachment; filename="Movie.en.srt"',
        "fallback",
      ),
    ).toBe("Movie.en.srt");
  });

  it("parses the unquoted form", () => {
    expect(
      filenameFromContentDisposition(
        "attachment; filename=Movie.en.srt",
        "fallback",
      ),
    ).toBe("Movie.en.srt");
  });

  it("prefers the RFC 5987 extended form and decodes it", () => {
    expect(
      filenameFromContentDisposition(
        "attachment; filename*=UTF-8''K%C3%A9m%20-%20subtitles.zip",
        "fallback",
      ),
    ).toBe("Kém - subtitles.zip");
  });

  it("keeps a semicolon inside a quoted filename", () => {
    expect(
      filenameFromContentDisposition(
        'attachment; filename="Rock; Roll - subtitles.zip"',
        "fallback",
      ),
    ).toBe("Rock; Roll - subtitles.zip");
  });

  it("unescapes quoted-pairs inside a quoted filename", () => {
    expect(
      filenameFromContentDisposition(
        'attachment; filename="a\\"b.srt"',
        "fallback",
      ),
    ).toBe('a"b.srt');
    expect(
      filenameFromContentDisposition(
        'attachment; filename="back\\\\slash.srt"',
        "fallback",
      ),
    ).toBe("back\\slash.srt");
  });

  it("falls back when the header is missing or unparsable", () => {
    expect(filenameFromContentDisposition(undefined, "fallback")).toBe(
      "fallback",
    );
    expect(filenameFromContentDisposition(null, "fallback")).toBe("fallback");
    expect(filenameFromContentDisposition("attachment", "fallback")).toBe(
      "fallback",
    );
  });

  it("survives malformed percent-encoding in the extended form", () => {
    expect(
      filenameFromContentDisposition(
        "attachment; filename*=UTF-8''bad%zz; filename=\"plain.srt\"",
        "fallback",
      ),
    ).toBe("plain.srt");
  });
});
