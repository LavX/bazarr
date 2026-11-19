/* eslint-disable camelcase -- transport names. */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import queryClient from "@/apis/queries";
import type { PluginCatalogSource } from "@/apis/raw/plugins";
import type {
  ProviderHubCatalogEntry,
  ProviderHubInstallation,
} from "@/apis/raw/providerHub";
import { MarketplacePanel } from "@/pages/Settings/Providers/hub/MarketplacePanel";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import { CatalogCard } from "./CatalogCard";

const schemaTwo = (
  manifest: Record<string, unknown> = {},
  over: Partial<ProviderHubCatalogEntry> = {},
) =>
  ({
    source: "official",
    source_name: "Official",
    provider_id: "relay",
    name: "Relay Subs",
    version: "2.0.0",
    trusted: true,
    manifest: {
      schema_version: 2,
      id: "relay",
      kind: "subtitle_provider",
      name: "Relay Subs",
      version: "2.0.0",
      description: "Finds subtitles through the Relay service.",
      permissions: ["network", "data_dir"],
      endpoints: ["https://api.relay.example:443"],
      requires: ["flaresolverr.v1"],
      provides: ["relay.search.v1"],
      ...manifest,
    },
    ...over,
  }) as unknown as ProviderHubCatalogEntry;

const schemaOne = {
  source: "official",
  source_name: "Official",
  provider_id: "legacy",
  name: "Legacy Subs",
  version: "1.0.0",
  trusted: true,
  manifest: {
    schema_version: 1,
    provider_id: "legacy",
    name: "Legacy Subs",
    version: "1.0.0",
  },
} as unknown as ProviderHubCatalogEntry;

const official = {
  id: "official",
  name: "Official",
  url: "https://github.com/LavX/bazarr-provider-catalog",
  trusted: true,
  official: true,
  catalog_signature: "verified",
  signature_verified: true,
} as PluginCatalogSource;

const community = {
  id: "community",
  name: "Community",
  url: "https://github.com/someone/catalog",
  trusted: false,
  official: false,
} as PluginCatalogSource;

let sandbox: Record<string, unknown>;

beforeEach(() => {
  queryClient.clear();
  // The host as the backend reports it today: plugin workers are not sandboxed, and the provider
  // profile confines the folders of a subtitle provider.
  sandbox = {
    workers_sandboxed: false,
    network: "declared",
    filesystem: "declared",
    label: "Sandbox available. Not active for plugin workers yet.",
    pending: false,
    providers: {
      enabled: true,
      state: "enforced",
      notice: null,
      unsandboxed: [],
      limitations: [],
    },
  };
  server.use(
    http.get("/api/system/status", () =>
      HttpResponse.json({ data: { sandbox } }),
    ),
  );
});

const show = (
  entry: ProviderHubCatalogEntry,
  source: PluginCatalogSource | undefined = official,
  extra: {
    installed?: ProviderHubInstallation | null;
    isLocal?: boolean;
  } = {},
) =>
  customRender(
    <CatalogCard
      entry={entry}
      installed={extra.installed ?? null}
      onInstall={vi.fn()}
      isInstalling={false}
      isLocal={extra.isLocal}
      source={source}
    />,
  );

const toggle = () => screen.getByRole("button", { name: /Declares/ });

const open = async () => {
  const user = userEvent.setup();
  await user.click(toggle());
  return screen.getByRole("region", { name: "What Relay Subs declares" });
};

describe("a schema 2 provider on its Subtitle Hub card", () => {
  it("says what it declares in one line and keeps the rest closed", () => {
    show(schemaTwo());

    expect(toggle()).toHaveAttribute("aria-expanded", "false");
    expect(toggle()).toHaveTextContent(
      "Declares 2 permissions (declared only) · 1 destination · needs 1 capability · supplies 1 capability",
    );
    expect(screen.queryByRole("region")).not.toBeInTheDocument();
  });

  it("counts the marks of this host's sandbox in that line", async () => {
    show(schemaTwo());

    await waitFor(() =>
      expect(toggle()).toHaveTextContent("2 permissions (1 enforced)"),
    );
  });

  it("opens the permissions with their marks, the destinations, the capabilities and the source", async () => {
    show(schemaTwo());
    await waitFor(() => expect(toggle()).toHaveTextContent("(1 enforced)"));

    const region = await open();

    expect(toggle()).toHaveAttribute("aria-expanded", "true");
    expect(toggle()).toHaveAttribute("aria-controls", region.id);
    const may = within(
      within(region).getByRole("group", { name: "What it may do" }),
    );
    const [network, data] = may.getAllByRole("listitem");
    expect(network).toHaveTextContent("Network access");
    expect(network).toHaveTextContent("Declared only");
    expect(data).toHaveTextContent("Keep its own data folder");
    expect(data).toHaveTextContent("Enforced");
    expect(
      may.getByText(/Declared only means the plugin says it needs it/),
    ).toBeVisible();

    const leaves = within(
      within(region).getByRole("group", { name: "What leaves this machine" }),
    );
    expect(leaves.getByText("https://api.relay.example:443")).toBeVisible();

    const capabilities = within(
      within(region).getByRole("group", { name: "Capabilities" }),
    );
    const [needs, supplies] = capabilities.getAllByRole("listitem");
    expect(needs).toHaveTextContent("NeedsFlareSolverr flaresolverr.v1");
    expect(supplies).toHaveTextContent("Suppliesrelay.search.v1");

    const origin = within(
      within(region).getByRole("group", { name: "Where it comes from" }),
    );
    expect(origin.getByText("Official")).toBeVisible();
    expect(
      origin.getByText("The official catalog, which Bazarr+ trusts."),
    ).toBeVisible();
    expect(
      origin.getByText(
        "Its catalog is signed, and Bazarr+ checked the signature.",
      ),
    ).toBeVisible();
    expect(
      screen.getByRole("status", { name: /^Signed catalog/ }),
    ).toBeVisible();
  });

  it("opens and closes from the keyboard", async () => {
    show(schemaTwo());
    const user = userEvent.setup();

    toggle().focus();
    await user.keyboard("{Enter}");
    expect(screen.getByRole("region", { name: /declares/ })).toBeVisible();
    await user.keyboard(" ");
    expect(screen.queryByRole("region")).not.toBeInTheDocument();
    expect(toggle()).toHaveFocus();
  });

  it("never calls the network enforced for a provider, whatever the plugin sandbox says", async () => {
    sandbox = {
      ...sandbox,
      workers_sandboxed: true,
      network: "enforced",
      filesystem: "enforced",
    };
    show(schemaTwo());

    const region = await open();

    const [network, data] = within(
      within(region).getByRole("group", { name: "What it may do" }),
    ).getAllByRole("listitem");
    expect(network).toHaveTextContent("Declared only");
    expect(data).toHaveTextContent("Enforced");
  });

  it("calls nothing enforced, and says why, when the provider sandbox is not applied", async () => {
    sandbox = {
      ...sandbox,
      providers: {
        enabled: true,
        state: "unsandboxed",
        notice:
          "Subtitle provider plugins run without the sandbox on this host, so nothing keeps them from reading the configuration or the database: this host has no Landlock.",
        unsandboxed: [],
        limitations: [],
      },
    };
    show(schemaTwo());

    const region = await open();

    expect(within(region).queryByText("Enforced")).not.toBeInTheDocument();
    expect(within(region).getAllByText("Declared only")).toHaveLength(2);
    expect(within(region).getByRole("note")).toHaveTextContent(
      "Subtitle provider plugins run without the sandbox on this host",
    );
  });

  it("calls a sandbox with gaps declared, with the gaps named", async () => {
    sandbox = {
      ...sandbox,
      providers: {
        enabled: true,
        state: "partial",
        notice: "The sandbox has gaps: an older Landlock ABI.",
        unsandboxed: [],
        limitations: [],
      },
    };
    show(schemaTwo());

    const region = await open();

    expect(within(region).queryByText("Enforced")).not.toBeInTheDocument();
    expect(within(region).getByRole("note")).toHaveTextContent(
      "an older Landlock ABI",
    );
  });

  it("says so when the provider sandbox is switched off", async () => {
    sandbox = {
      ...sandbox,
      providers: {
        enabled: false,
        state: "off",
        notice: null,
        unsandboxed: [],
        limitations: [],
      },
    };
    show(schemaTwo());

    const region = await open();

    expect(within(region).queryByText("Enforced")).not.toBeInTheDocument();
    expect(within(region).getByRole("note")).toHaveTextContent(
      "The provider sandbox is switched off",
    );
  });

  it("says a network permission with no fixed address leaves the address open", async () => {
    show(schemaTwo({ endpoints: [], permissions: ["network"] }));
    expect(toggle()).toHaveTextContent("no fixed destination");

    const region = await open();

    expect(
      within(region).getByText(
        "It can reach the network, and the plugin does not fix which addresses.",
      ),
    ).toBeVisible();
  });

  it("says nothing leaves the machine when it has no network permission, and names a program it runs", async () => {
    show(
      schemaTwo({
        permissions: ["binaries"],
        endpoints: [],
        requires: [],
        provides: [],
        runtime: { artifact: "archive", process: "worker" },
      }),
    );

    const region = await open();

    expect(
      within(region).getByText("Nothing is sent from this machine."),
    ).toBeVisible();
    expect(
      within(region).getByText(/Runs a program it downloads/),
    ).toBeVisible();
    expect(
      within(region).queryByRole("group", { name: "Capabilities" }),
    ).not.toBeInTheDocument();
  });

  it("mentions an address the person enters in its settings", async () => {
    show(
      schemaTwo({
        settings: { fields: [{ key: "url", type: "text", destination: true }] },
        settings_version: 1,
      }),
    );

    const region = await open();

    expect(
      within(region).getByText(
        "It also connects to an address you enter in its settings.",
      ),
    ).toBeVisible();
  });

  it("shows a community source as unsigned and untrusted, in words", async () => {
    show(
      schemaTwo(
        {},
        { source: "community", source_name: "Community", trusted: false },
      ),
      community,
    );

    const region = await open();

    expect(
      within(region).getByText(
        "A community catalog source. Review it before you install.",
      ),
    ).toBeVisible();
    expect(
      within(region).getByText(
        "Its catalog is not signed. Bazarr+ checks signatures only on its official catalog.",
      ),
    ).toBeVisible();
    expect(screen.getByText("Unsigned catalog")).toBeVisible();
    expect(
      screen.getByRole("status", { name: "Community source" }),
    ).toBeVisible();
  });

  it("says a legacy official catalog is unsigned, not that it failed", async () => {
    show(schemaTwo(), {
      ...official,
      catalog_signature: "unsigned (legacy)",
      signature_verified: false,
    });

    const region = await open();

    expect(
      within(region).getByText(
        "Its catalog is not signed yet. This is the older catalog format.",
      ),
    ).toBeVisible();
  });

  it("does not claim a signature check for an official catalog with no signature record yet", async () => {
    show(schemaTwo(), {
      ...official,
      catalog_signature: undefined,
      signature_verified: undefined,
    });

    const region = await open();

    expect(
      within(region).getByText(
        "Its catalog is not signed yet. This is the older catalog format.",
      ),
    ).toBeVisible();
    expect(
      within(region).queryByText(/checks signatures only on its official/),
    ).not.toBeInTheDocument();
    expect(screen.getByText("Unsigned catalog")).toBeVisible();
  });

  it("does not call an official source trusted when the entry is not", async () => {
    show(schemaTwo({}, { trusted: false }), {
      ...official,
      trust_reason: "outside main and beta",
    });

    const region = await open();

    expect(
      within(region).queryByText("The official catalog, which Bazarr+ trusts."),
    ).not.toBeInTheDocument();
    expect(
      within(region).getByText(/not trusted: outside main and beta/i),
    ).toBeVisible();
  });

  it("says Bazarr+ has no record of the source when it is gone", async () => {
    show(
      schemaTwo({}, { source: undefined, source_name: "Unknown source" }),
      undefined,
    );

    const region = await open();

    expect(
      within(region).getByText(/no record of where this came from/i),
    ).toBeVisible();
    expect(
      within(region).queryByText(/community catalog source/i),
    ).not.toBeInTheDocument();
  });

  it("says a development source has not been reviewed", async () => {
    show(
      schemaTwo({}, {
        development_source: true,
      } as Partial<ProviderHubCatalogEntry>),
      community,
    );

    const region = await open();

    expect(
      within(region).getByText(
        "A development source. Its code has not been reviewed.",
      ),
    ).toBeVisible();
  });

  it("describes an uploaded package without a signature", async () => {
    show(schemaTwo(), undefined, { isLocal: true });

    const region = await open();

    expect(
      within(region).getByText(
        /An uploaded package\. It belongs to no catalog/,
      ),
    ).toBeVisible();
    expect(screen.queryByText(/catalog$/)).not.toBeInTheDocument();
  });

  it("keeps the revoked state of the card, with the reason", () => {
    show(schemaTwo(), official, {
      installed: {
        provider_id: "relay",
        name: "Relay Subs",
        active_version: "2.0.0",
        state: "active",
        needs_restart: true,
        revoked: true,
        revoked_reason: "Version 2.0.0 was revoked.",
        trusted: true,
        source_id: "official",
        source_bound: true,
      } as unknown as ProviderHubInstallation,
    });

    expect(
      screen.getByRole("status", { name: /^Revoked \(danger\)/ }),
    ).toHaveAccessibleName(/Version 2\.0\.0 was revoked\./);
    expect(toggle()).toBeVisible();
  });

  it("keeps the awaiting-restart state of the card", () => {
    show(schemaTwo(), official, {
      installed: {
        provider_id: "relay",
        name: "Relay Subs",
        staged_version: "2.0.0",
        state: "staged",
        needs_restart: true,
        trusted: true,
        source_id: "official",
        source_bound: true,
      } as unknown as ProviderHubInstallation,
    });

    expect(screen.getByText("Pending restart")).toBeVisible();
    expect(toggle()).toBeVisible();
  });
});

describe("a schema 1 provider on its Subtitle Hub card", () => {
  it("shows no declaration line and no signature pill, and never asks for the sandbox", () => {
    let asked = false;
    server.use(
      http.get("/api/system/status", () => {
        asked = true;
        return HttpResponse.json({ data: { sandbox } });
      }),
    );

    show(schemaOne);

    expect(screen.getByText("Legacy Subs")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: /Declares/ }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/catalog$/)).not.toBeInTheDocument();
    expect(asked).toBe(false);
  });
});

describe("the Marketplace of the Subtitle Hub", () => {
  it("hands each schema 2 card the record of its own source", async () => {
    customRender(
      <MarketplacePanel
        catalog={{
          sources: [official, community],
          entries: [
            schemaTwo(),
            schemaTwo(
              { id: "other", name: "Other Subs" },
              {
                provider_id: "other",
                name: "Other Subs",
                source: "community",
                source_name: "Community",
                trusted: false,
              },
            ),
            schemaOne,
          ],
        }}
        providers={[]}
      />,
    );

    expect(
      await screen.findAllByRole("button", { name: /Declares/ }),
    ).toHaveLength(2);
    expect(screen.getAllByText("Signed catalog")).toHaveLength(1);
    expect(screen.getAllByText("Unsigned catalog")).toHaveLength(1);
  });
});
