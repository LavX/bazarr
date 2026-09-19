import { render, screen } from "@testing-library/react";
import SubtitleCueText from "./SubtitleCueText";

describe("subtitle formatting", () => {
  it("renders nested, unclosed formatting and safe font colors", () => {
    render(
      <SubtitleCueText
        text={
          '<i>Hello <b>world</b></i> <font color="#ff8800">colour</font> <u>unfinished'
        }
      />,
    );
    expect(screen.getByText("world").tagName).toBe("B");
    expect(screen.getByText("Hello", { exact: false }).tagName).toBe("I");
    expect(screen.getByText("colour")).toHaveStyle({ color: "#ff8800" });
    expect(screen.getByText("unfinished").tagName).toBe("U");
  });

  it("treats unknown markup as text without creating executable elements", () => {
    render(
      <div data-testid="cue">
        <SubtitleCueText
          text={
            '<script>untrusted()</script><img src=x onerror="untrusted()"><i onclick="untrusted()">safe</i><unknown>readable</unknown><font color="url(javascript:bad)">plain</font>'
          }
        />
      </div>,
    );
    const cue = screen.getByTestId("cue");
    expect(cue).toHaveTextContent("untrusted()safereadableplain");
    expect(cue.innerHTML).not.toMatch(/script|onerror|onclick|<img|url\(/i);
    expect(screen.getByText("safe").tagName).toBe("I");
  });

  it("bounds formatting depth and tolerates broken provider markup", () => {
    render(
      <SubtitleCueText text={"<i>".repeat(1000) + "still readable" + "</b>"} />,
    );
    expect(screen.getByText("still readable")).toBeVisible();
  });
});

it("preserves dialogue containing less-than signs and incomplete tags", () => {
  render(<SubtitleCueText text="If x<y, move left. <i unfinished words" />);
  expect(
    screen.getByText("If x<y, move left. <i unfinished words"),
  ).toBeVisible();
});

it("preserves comparison dialogue before a later formatting tag", () => {
  render(
    <div role="note">
      <SubtitleCueText text="If x<y, move left. <i>carefully</i>" />
    </div>,
  );
  expect(screen.getByRole("note")).toHaveTextContent(
    "If x<y, move left. carefully",
  );
  expect(screen.getByText("carefully").tagName).toBe("I");
});
