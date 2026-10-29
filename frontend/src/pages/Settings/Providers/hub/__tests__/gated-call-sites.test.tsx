/* eslint-disable camelcase */

/**
 * Call-site tests for the gated Provider Hub actions: the marketplace install
 * and local upload, and the catalog source add and ref change.
 *
 * Why: all four are gated behind the password confirmation on the backend, so
 * each call site routes its action through the gated action: with login on and
 * no live window the prompt appears before the request rather than as a
 * refusal toast after it.
 *
 * Test: Run with `cd frontend && pnpm exec vitest run --reporter=verbose`.
 */

import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import { forgetPasswordConfirmation } from "@/modules/passwordConfirm";
import { MarketplacePanel } from "@/pages/Settings/Providers/hub/MarketplacePanel";
import { SourcesDrawer } from "@/pages/Settings/Providers/hub/SourcesDrawer";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";

const manifest = {
  schema_version: 1,
  provider_id: "someprovider",
  name: "Some Provider",
  version: "1.0.0",
  source: {
    type: "github",
    repo: "LavX/bazarr-provider-catalog",
    ref: "main",
    commit: "a".repeat(40),
    trusted: true,
  },
};

const catalog = {
  sources: [
    {
      id: "official",
      name: "Official",
      type: "github",
      url: "https://github.com/LavX/bazarr-provider-catalog/blob/main/catalog.json",
      enabled: true,
      trusted: true,
      official: true,
      last_error: null,
    },
  ],
  entries: [
    {
      source: "official",
      provider_id: "someprovider",
      name: "Some Provider",
      version: "1.0.0",
      trusted: true,
      manifest,
    },
  ],
};

function mockFormLogin() {
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "auto" },
        auth: { type: "form", username: "admin", password: "***" },
      }),
    ),
    http.post(
      "/api/system/account/confirm",
      () => new HttpResponse(null, { status: 204 }),
    ),
  );
}

// Every gated Hub action queues a backend job the client then waits on; a
// null job id settles the wait at once in the test.
function mockJobEndpoints() {
  server.use(
    http.post("/api/provider-hub/installations", () =>
      HttpResponse.json({ job_id: null }),
    ),
    http.post("/api/provider-hub/installations/local", () =>
      HttpResponse.json({ job_id: null }),
    ),
  );
}

async function confirmThePassword(user: ReturnType<typeof userEvent.setup>) {
  const prompt = await screen.findByRole("dialog", {
    name: "Confirm your password",
  });
  await user.type(within(prompt).getByLabelText("Password"), "hunter2");
  await user.click(within(prompt).getByRole("button", { name: /^confirm$/i }));
}

describe("MarketplacePanel and the password confirmation", () => {
  beforeEach(() => {
    forgetPasswordConfirmation();
    mockFormLogin();
    mockJobEndpoints();
  });

  it("prompts before installing a catalog provider, then installs", async () => {
    const user = userEvent.setup();
    const installs: Record<string, unknown>[] = [];
    server.use(
      http.post("/api/provider-hub/installations", async ({ request }) => {
        installs.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json({ job_id: null });
      }),
    );

    customRender(<MarketplacePanel catalog={catalog} providers={[]} />);

    await user.click(await screen.findByRole("button", { name: /^install$/i }));

    expect(installs).toEqual([]);
    expect(
      await screen.findByText("Confirm your password"),
    ).toBeInTheDocument();

    await confirmThePassword(user);

    await waitFor(() => {
      expect(installs).toEqual([
        { source: "official", provider_id: "someprovider", version: "1.0.0" },
      ]);
    });
  });

  it("prompts before uploading a local package, then uploads", async () => {
    const user = userEvent.setup();
    const uploads: string[] = [];
    server.use(
      http.post("/api/provider-hub/installations/local", () => {
        // The multipart body is opaque in jsdom (the File never gains a
        // boundary there), so the request itself is what is recorded: the
        // assertion cares that the upload waited for the confirmation.
        uploads.push("uploaded");
        return HttpResponse.json({ job_id: null });
      }),
    );

    customRender(<MarketplacePanel catalog={catalog} providers={[]} />);

    // The FileButton's hidden input carries no label, so it is reached the
    // same way the browser does: through its type.
    // eslint-disable-next-line testing-library/no-node-access
    const input = document.querySelector("input[type=file]");
    if (input === null) throw new Error("The upload input did not render");
    const file = new File(["a package"], "plugin.zip", {
      type: "application/zip",
    });
    await user.upload(input as HTMLInputElement, file);

    expect(uploads).toEqual([]);
    expect(
      await screen.findByText("Confirm your password"),
    ).toBeInTheDocument();

    await confirmThePassword(user);

    await waitFor(() => {
      expect(uploads).toEqual(["uploaded"]);
    });
  });
});

describe("SourcesDrawer and the password confirmation", () => {
  beforeEach(() => {
    forgetPasswordConfirmation();
    mockFormLogin();
  });

  it("prompts before adding a catalog source, then adds it", async () => {
    const user = userEvent.setup();
    const added: Record<string, unknown>[] = [];
    server.use(
      http.post("/api/provider-hub/catalog/sources", async ({ request }) => {
        added.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json({ name: "community" });
      }),
    );

    customRender(
      <SourcesDrawer
        opened
        onClose={() => undefined}
        sources={catalog.sources}
        installedSourceUsage={{}}
      />,
    );

    await user.type(
      await screen.findByRole("textbox", { name: /github catalog url/i }),
      "https://github.com/someone/community/blob/main/catalog.json",
    );
    const nameInput = screen.getByRole("textbox", { name: /^name$/i });
    // The URL auto-fills the suggested name until the field is touched.
    await user.clear(nameInput);
    await user.type(nameInput, "community");
    await user.click(
      await screen.findByRole("button", { name: /add source/i }),
    );

    expect(added).toEqual([]);
    expect(
      await screen.findByText("Confirm your password"),
    ).toBeInTheDocument();

    await confirmThePassword(user);

    await waitFor(() => {
      expect(added).toEqual([
        {
          name: "community",
          url: "https://github.com/someone/community/blob/main/catalog.json",
        },
      ]);
    });
  });

  it("prompts before changing a source ref, then changes it", async () => {
    const user = userEvent.setup();
    const patched: Record<string, unknown>[] = [];
    server.use(
      http.patch(
        "/api/provider-hub/catalog/sources/:name",
        async ({ request, params }) => {
          patched.push({ name: params.name, body: await request.json() });
          return HttpResponse.json({ id: params.name });
        },
      ),
    );

    customRender(
      <SourcesDrawer
        opened
        onClose={() => undefined}
        sources={catalog.sources}
        installedSourceUsage={{}}
      />,
    );

    await user.click(await screen.findByRole("switch", { name: /dev mode/i }));
    const branchInput = screen.getByRole("textbox", { name: /branch/i });
    // The branch starts at the source's stable ref; the change replaces it.
    await user.clear(branchInput);
    await user.type(branchInput, "feat/branch");
    await user.click(screen.getByRole("button", { name: /^apply$/i }));

    expect(patched).toEqual([]);
    expect(
      await screen.findByText("Confirm your password"),
    ).toBeInTheDocument();

    await confirmThePassword(user);

    await waitFor(() => {
      expect(patched).toEqual([
        { name: "official", body: { dev_ref: "feat/branch" } },
      ]);
    });
  });
});
