import { Button } from "@mantine/core";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  PasswordConfirmView,
  useGatedAction,
} from "@/components/modals/PasswordConfirmModal";
import {
  forgetPasswordConfirmation,
  markPasswordConfirmed,
  passwordConfirmWindowLive,
} from "@/modules/passwordConfirm";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

const FORM_AUTH_SETTINGS = http.get("/api/system/settings", () =>
  HttpResponse.json({
    general: { theme: "auto" },
    auth: { type: "form", username: "admin", password: "***" },
  }),
);

function mockConfirmEndpoint(status = 204) {
  const bodies: Record<string, string>[] = [];
  server.use(
    http.post("/api/system/account/confirm", async ({ request }) => {
      const submitted: Record<string, string> = {};
      for (const [key, value] of (await request.formData()).entries()) {
        submitted[key] = String(value);
      }
      bodies.push(submitted);
      return new HttpResponse(null, { status });
    }),
  );
  return bodies;
}

describe("PasswordConfirmView", () => {
  beforeEach(() => {
    forgetPasswordConfirmation();
  });

  it("confirms with the settings username and the typed password", async () => {
    const user = userEvent.setup();
    const onConfirmed = vi.fn();
    server.use(FORM_AUTH_SETTINGS);
    const bodies = mockConfirmEndpoint();

    customRender(
      <PasswordConfirmView
        action="install this provider"
        onConfirmed={onConfirmed}
      />,
    );

    await user.type(screen.getByLabelText("Password"), "hunter2");
    await user.click(screen.getByRole("button", { name: /^confirm$/i }));

    await waitFor(() => expect(onConfirmed).toHaveBeenCalled());
    expect(bodies).toEqual([{ username: "admin", password: "hunter2" }]);
    expect(passwordConfirmWindowLive()).toBe(true);
  });

  it("asks for the password without calling the server when it is empty", async () => {
    const user = userEvent.setup();
    const onConfirmed = vi.fn();
    server.use(FORM_AUTH_SETTINGS);
    const bodies = mockConfirmEndpoint(403);

    customRender(
      <PasswordConfirmView
        action="install this provider"
        onConfirmed={onConfirmed}
      />,
    );

    await user.click(screen.getByRole("button", { name: /^confirm$/i }));

    expect(await screen.findByText("Enter your password.")).toBeInTheDocument();
    // An empty submit never reaches the endpoint, so it cannot spend one of
    // the attempts the login throttle allows before it locks the address out.
    expect(bodies).toEqual([]);
    expect(onConfirmed).not.toHaveBeenCalled();
    expect(passwordConfirmWindowLive()).toBe(false);
  });

  it("shows an inline error for a wrong password and stays open", async () => {
    const user = userEvent.setup();
    const onConfirmed = vi.fn();
    server.use(FORM_AUTH_SETTINGS);
    mockConfirmEndpoint(403);

    customRender(
      <PasswordConfirmView
        action="restore this backup"
        onConfirmed={onConfirmed}
      />,
    );

    await user.type(screen.getByLabelText("Password"), "wrong");
    await user.click(screen.getByRole("button", { name: /^confirm$/i }));

    expect(
      await screen.findByText("The password is not correct."),
    ).toBeInTheDocument();
    expect(onConfirmed).not.toHaveBeenCalled();
    expect(passwordConfirmWindowLive()).toBe(false);
    // The refusal is answered inline only: the shared client was asked to
    // stay quiet, so there is no toast on top of the modal's message.
    expect(screen.queryByText("Error 403")).not.toBeInTheDocument();
    expect(screen.queryByText("Authentication failed")).not.toBeInTheDocument();
  });

  it("answers the throttle inline too, without a second message", async () => {
    const user = userEvent.setup();
    const onConfirmed = vi.fn();
    server.use(FORM_AUTH_SETTINGS);
    mockConfirmEndpoint(429);

    customRender(
      <PasswordConfirmView
        action="install this provider"
        onConfirmed={onConfirmed}
      />,
    );

    await user.type(screen.getByLabelText("Password"), "hunter2");
    await user.click(screen.getByRole("button", { name: /^confirm$/i }));

    expect(
      await screen.findByText(
        "Too many attempts. Wait five minutes and try again.",
      ),
    ).toBeInTheDocument();
    expect(onConfirmed).not.toHaveBeenCalled();
    expect(screen.queryByText("Error 429")).not.toBeInTheDocument();
  });
});

function Probe({ onRun }: { onRun: () => void }) {
  const runGated = useGatedAction();
  return (
    <Button onClick={() => void runGated(onRun, "run the probe")}>Go</Button>
  );
}

describe("useGatedAction", () => {
  beforeEach(() => {
    forgetPasswordConfirmation();
  });

  it("runs the action with no prompt when no login mode is configured", async () => {
    const user = userEvent.setup();
    const onRun = vi.fn();
    // The default settings mock carries no auth section: login is off.
    customRender(<Probe onRun={onRun} />);

    await user.click(screen.getByRole("button", { name: /go/i }));

    await waitFor(() => expect(onRun).toHaveBeenCalled());
    expect(screen.queryByText("Confirm your password")).not.toBeInTheDocument();
  });

  it("runs the action with no prompt inside the remembered window", async () => {
    const user = userEvent.setup();
    const onRun = vi.fn();
    server.use(FORM_AUTH_SETTINGS);
    markPasswordConfirmed();

    customRender(<Probe onRun={onRun} />);

    await user.click(screen.getByRole("button", { name: /go/i }));

    await waitFor(() => expect(onRun).toHaveBeenCalled());
    expect(screen.queryByText("Confirm your password")).not.toBeInTheDocument();
  });

  it("prompts before the action and runs it after a confirmation", async () => {
    const user = userEvent.setup();
    const onRun = vi.fn();
    server.use(FORM_AUTH_SETTINGS);
    mockConfirmEndpoint();

    customRender(<Probe onRun={onRun} />);
    await user.click(screen.getByRole("button", { name: /go/i }));

    expect(onRun).not.toHaveBeenCalled();
    expect(
      await screen.findByText("Confirm your password"),
    ).toBeInTheDocument();

    await user.type(screen.getByLabelText("Password"), "hunter2");
    await user.click(screen.getByRole("button", { name: /^confirm$/i }));

    await waitFor(() => expect(onRun).toHaveBeenCalled());
  });

  it("blocks the action with a retryable error when the refetch cannot answer", async () => {
    const user = userEvent.setup();
    const onRun = vi.fn();
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({}, { status: 500 }),
      ),
    );

    customRender(<Probe onRun={onRun} />);

    await user.click(screen.getByRole("button", { name: /go/i }));

    expect(onRun).not.toHaveBeenCalled();
    expect(
      await screen.findByText(
        "Bazarr+ could not read its settings, so it cannot tell whether a password is needed. Try again.",
      ),
    ).toBeInTheDocument();

    // The error is retryable: closing it and invoking the gated action again
    // repeats the read, and once the settings answer, the action runs.
    await user.click(screen.getByRole("button", { name: /cancel/i }));
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({ general: { theme: "auto" } }),
      ),
    );
    await user.click(screen.getByRole("button", { name: /go/i }));

    // No password was ever typed, so the run can only have come from a
    // read that finally answered: the retry needs no prompt.
    await waitFor(() => expect(onRun).toHaveBeenCalledTimes(1));
  });
});
