import { describe, expect, it } from "vitest";
import { antiCaptchaOption } from "@/pages/Settings/Providers/options";

describe("anti-captcha vendor options", () => {
  it("offers CaptchaAI with the backend's validator value", () => {
    const captchaai = antiCaptchaOption.find((o) => o.value === "captchaai");
    expect(captchaai).toBeDefined();
    expect(captchaai?.label).toBe("CaptchaAI");
  });

  it("keeps the existing vendors selectable", () => {
    const values = antiCaptchaOption.map((o) => o.value);
    expect(values).toContain("anti-captcha");
    expect(values).toContain("death-by-captcha");
  });
});
