import { fromPython, progressPercent, toPython } from "@/utilities/index";

describe("fromPythonConversion", () => {
  it("should convert a true value", () => {
    expect(fromPython("True")).toBe(true);
  });

  it("should convert a false value", () => {
    expect(fromPython("False")).toBe(false);
  });

  it("should convert an undefined value", () => {
    expect(fromPython(undefined)).toBe(false);
  });
});

describe("toPythonConversion", () => {
  it("should convert a true value", () => {
    expect(toPython(true)).toBe("True");
  });

  it("should convert a false value", () => {
    expect(toPython(false)).toBe("False");
  });
});

describe("progressPercent", () => {
  it("computes a normal percentage", () => {
    expect(progressPercent(2, 4)).toBe(50);
  });

  it("clamps values above the max to 100", () => {
    expect(progressPercent(179, 3)).toBe(100);
  });

  it("returns 0 when max is 0", () => {
    expect(progressPercent(5, 0)).toBe(0);
  });

  it("floors negative results at 0", () => {
    expect(progressPercent(-1, 10)).toBe(0);
  });
});
