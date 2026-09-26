import { afterEach, describe, expect, it } from "vitest";
import {
  readSessionValue,
  readStoredValue,
  removeSessionValue,
  removeStoredValue,
  writeSessionValue,
  writeStoredValue,
} from "@/utilities/browserStorage";

/**
 * The whole reason this module exists is the catch. A browser that blocks site
 * data throws on the property access itself, not only on the method call, so
 * both shapes are exercised here: a storage object whose methods throw, and an
 * area whose getter throws before any method is reached.
 */
const areas = ["localStorage", "sessionStorage"] as const;
type Area = (typeof areas)[number];

const originals = new Map<Area, PropertyDescriptor>();

function replaceArea(area: Area, get: () => unknown) {
  if (!originals.has(area)) {
    const descriptor = Object.getOwnPropertyDescriptor(window, area);
    if (descriptor) originals.set(area, descriptor);
  }
  Object.defineProperty(window, area, { configurable: true, get });
}

afterEach(() => {
  for (const [area, descriptor] of originals) {
    Object.defineProperty(window, area, descriptor);
  }
  originals.clear();
});

const api = {
  localStorage: {
    read: readStoredValue,
    write: writeStoredValue,
    remove: removeStoredValue,
  },
  sessionStorage: {
    read: readSessionValue,
    write: writeSessionValue,
    remove: removeSessionValue,
  },
} as const;

describe.each(areas)("%s", (area) => {
  const { read, write, remove } = api[area];

  it("reads and writes through when storage works", () => {
    expect(write("bazarr.test.key", "value")).toBe(true);
    expect(read("bazarr.test.key")).toBe("value");
    remove("bazarr.test.key");
    expect(read("bazarr.test.key")).toBeNull();
  });

  it("returns null, false and nothing when every method throws", () => {
    const blocked = {
      getItem() {
        throw new Error("storage blocked");
      },
      setItem() {
        throw new Error("storage blocked");
      },
      removeItem() {
        throw new Error("storage blocked");
      },
    };
    replaceArea(area, () => blocked);
    expect(read("bazarr.test.key")).toBeNull();
    expect(write("bazarr.test.key", "value")).toBe(false);
    expect(() => remove("bazarr.test.key")).not.toThrow();
  });

  it("survives an area whose own property access throws", () => {
    replaceArea(area, () => {
      throw new Error("storage blocked");
    });
    expect(read("bazarr.test.key")).toBeNull();
    expect(write("bazarr.test.key", "value")).toBe(false);
    expect(() => remove("bazarr.test.key")).not.toThrow();
  });
});
