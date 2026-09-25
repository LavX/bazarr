import type { ProviderHubRuntimeStatus } from "@/apis/raw/providerHub";
import { customRender, screen } from "@/tests";
import { TranslationQuotaLine } from "./TranslationQuotaLine";

const base: ProviderHubRuntimeStatus = {
  entitled: true,
  exhausted: false,
  remaining: 37,
  limit: 50,
  reset_at: "2026-10-01",
  reported_at: "2026-09-24T10:00:00Z",
};

it.each([
  [{}, "AI translation: 37 of 50 left, resets 1 Oct"],
  [{ limit: null }, "AI translation: 37 left, resets 1 Oct"],
  [{ exhausted: true }, "AI translation quota used up, resets 1 Oct"],
  [{ entitled: false }, "AI translation is not included in this account"],
  [{ remaining: null, limit: null }, "AI translation available"],
] as const)("renders the reported quota state", (overrides, expected) => {
  customRender(<TranslationQuotaLine status={{ ...base, ...overrides }} />);
  expect(screen.getByText(expected)).toBeInTheDocument();
});

it("renders nothing before a worker reports quota", () => {
  customRender(<TranslationQuotaLine />);
  expect(screen.queryByText(/AI translation/)).not.toBeInTheDocument();
});

it.each([
  { entitled: null, exhausted: null },
  { entitled: true, exhausted: null },
] as const)(
  "renders nothing when the report states no usable quota fact",
  (overrides) => {
    customRender(
      <TranslationQuotaLine
        status={{
          entitled: overrides.entitled,
          exhausted: overrides.exhausted,
          remaining: null,
          limit: null,
          reset_at: "2026-10-01",
          reported_at: "2026-09-24T10:00:00Z",
        }}
      />,
    );
    expect(screen.queryByText(/AI translation/)).not.toBeInTheDocument();
  },
);
