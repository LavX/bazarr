import { act, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { customRender } from "@/tests";
import { FullPageDropzone } from "./FullPageDropzone";

function fileDragEvent(type: string, files: File[]) {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.defineProperty(event, "dataTransfer", {
    value: { files, types: ["Files"] },
  });
  return event;
}

describe("FullPageDropzone", () => {
  it("shows the overlay only while files are dragged over the window", () => {
    customRender(<FullPageDropzone active onDrop={() => {}} />);
    expect(screen.queryByText("Upload Subtitles")).toBeNull();
    act(() => {
      window.dispatchEvent(fileDragEvent("dragenter", []));
    });
    expect(screen.getByText("Upload Subtitles")).toBeInTheDocument();
  });

  it("calls onDrop with the dropped files and prevents the browser default", () => {
    const onDrop = vi.fn();
    customRender(<FullPageDropzone active onDrop={onDrop} />);
    const file = new File(["x"], "a.zip", { type: "application/zip" });
    const event = fileDragEvent("drop", [file]);
    const prevented = vi.spyOn(event, "preventDefault");
    act(() => {
      window.dispatchEvent(event);
    });
    expect(onDrop).toHaveBeenCalledTimes(1);
    expect((onDrop.mock.calls[0][0] as File[]).map((f) => f.name)).toEqual([
      "a.zip",
    ]);
    expect(prevented).toHaveBeenCalled();
  });

  it("ignores drops when inactive", () => {
    const onDrop = vi.fn();
    customRender(<FullPageDropzone active={false} onDrop={onDrop} />);
    act(() => {
      window.dispatchEvent(fileDragEvent("drop", [new File(["x"], "a.zip")]));
    });
    expect(onDrop).not.toHaveBeenCalled();
  });

  it("exposes an open() through openRef for the toolbar Upload button", () => {
    const openRef: { current: (() => void) | null } = { current: null };
    customRender(
      <FullPageDropzone active onDrop={() => {}} openRef={openRef} />,
    );
    expect(typeof openRef.current).toBe("function");
  });
});
