// Vitest 5 bundles its expect types and re-exports `Assertion` from an
// internal chunk, so the `Assertion` augmentation that jest-dom ships no
// longer merges and every DOM matcher becomes a type error. `Assertion`
// extends `Matchers`, the interface Vitest keeps open for custom matchers,
// so the jest-dom matchers are declared there instead, with the same type
// parameters Vitest uses. The runtime registration is still the jest-dom
// import in setup.tsx.
import type { TestingLibraryMatchers } from "@testing-library/jest-dom/matchers";
import "vitest";

declare module "vitest" {
  interface Matchers<
    R extends void | Promise<void> = void | Promise<void>,
    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    T = unknown,
  > extends TestingLibraryMatchers<unknown, R> {}
}
