/* eslint-disable camelcase -- API fixtures retain transport field names. */
import { createMemoryRouter, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import { AllProviders } from "@/providers";
import { rawRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import SettingsProvidersView from ".";

beforeEach(() => {
  queryClient.clear();
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({ general: { theme: "auto", enabled_providers: [] } }),
    ),
    http.get("/api/provider-hub/providers", () =>
      HttpResponse.json({ data: [] }),
    ),
    http.get("/api/provider-hub/catalog", () =>
      HttpResponse.json({ data: { sources: [], entries: [] } }),
    ),
    http.get("/api/provider-hub/jobs", () => HttpResponse.json({ data: [] })),
  );
});

// Discover's readiness notice links here with ?tab=marketplace. Reading that
// parameter only at mount meant the link worked from elsewhere and did nothing
// at all when this page was already open, which is exactly the case the notice
// creates once a reader has been here and switched tabs.
it("opens the marketplace tab from the link every time it is followed", async () => {
  const router = createMemoryRouter(
    [{ path: "/subtitle-hub", element: <SettingsProvidersView /> }],
    { initialEntries: ["/subtitle-hub?tab=marketplace"] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
  const user = userEvent.setup();
  expect(
    await screen.findByRole("tab", { name: /Marketplace/, selected: true }),
  ).toBeInTheDocument();
  await user.click(screen.getByRole("tab", { name: /My Providers/ }));
  expect(
    screen.getByRole("tab", { name: /My Providers/, selected: true }),
  ).toBeInTheDocument();
  await router.navigate("/subtitle-hub?tab=marketplace");
  expect(
    await screen.findByRole("tab", { name: /Marketplace/, selected: true }),
  ).toBeInTheDocument();
});

it("saves the AI subtitle penalty as a bounded integer setting", async () => {
  const submitted: FormData[] = [];
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: {
          theme: "auto",
          enabled_providers: [],
          ai_translated_score_penalty: 0,
        },
      }),
    ),
    http.post("/api/system/settings", async ({ request }) => {
      submitted.push(await request.formData());
      return new HttpResponse(null, { status: 204 });
    }),
  );
  const router = createMemoryRouter(
    [{ path: "/subtitle-hub", element: <SettingsProvidersView /> }],
    { initialEntries: ["/subtitle-hub"] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
  const user = userEvent.setup();
  const input = await screen.findByLabelText(
    "AI-translated subtitle penalty (%)",
  );
  await user.clear(input);
  await user.type(input, "10");
  await user.click(
    await screen.findByRole("button", { name: /Save 1 pending change/ }),
  );
  await waitFor(() => expect(submitted).toHaveLength(1));
  expect(submitted[0].get("settings-general-ai_translated_score_penalty")).toBe(
    "10",
  );
});

// The write-only states of a plugin secret on the provider card. The value
// never comes back from the API, so the card has to say what is stored
// through the descriptor, and every action has to be explicit: an
// untouched save keeps the stored key, only Clear stages the removal, and
// a replacement needs Replace pressed first (Cancel returns to Configured).
const secretHubProvider = {
  provider_id: "examplehub",
  name: "Example Hub",
  state: "active",
  active_version: "1.0.0",
  manifest: {
    id: "examplehub",
    name: "Example Hub",
    config_schema: {
      properties: {
        api_key: { type: "string", title: "API key", secret: true },
        region: { type: "string", title: "Region" },
      },
    },
  },
};

const secretSettings: Record<string, unknown> = {
  general: { theme: "auto", enabled_providers: ["examplehub"] },
  examplehub: {
    api_key: "",
    region: "eu",
    __secret_fields: { api_key: { configured: true, last_four: "1a2b" } },
  },
};

function mockProviderWithSecret(
  submitted: FormData[],
  settings: Record<string, unknown> = secretSettings,
  providers: unknown[] = [secretHubProvider],
) {
  server.use(
    http.get("/api/system/settings", () => HttpResponse.json(settings)),
    http.get("/api/provider-hub/providers", () =>
      HttpResponse.json({ data: providers }),
    ),
    http.post("/api/system/settings", async ({ request }) => {
      submitted.push(await request.formData());
      return new HttpResponse(null, { status: 204 });
    }),
  );
}

async function openProviderCard(
  submitted: FormData[] = [],
  settings: Record<string, unknown> = secretSettings,
  providers: unknown[] = [secretHubProvider],
  tileName: RegExp | string = /example hub/i,
  readyText: RegExp | string = /configured, ends in 1a2b/i,
) {
  mockProviderWithSecret(submitted, settings, providers);
  const user = userEvent.setup();
  const router = createMemoryRouter(
    [{ path: "/subtitle-hub", element: <SettingsProvidersView /> }],
    { initialEntries: ["/subtitle-hub"] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
  await user.click(await screen.findByRole("button", { name: tileName }));
  if (readyText) {
    await screen.findByText(readyText);
  }
  return user;
}

async function saveCard(
  user: Awaited<ReturnType<typeof userEvent.setup>>,
  submitted: FormData[],
) {
  await user.click(screen.getByRole("button", { name: "Save" }));
  await user.click(
    await screen.findByRole("button", { name: /save \d+ pending changes?/i }),
  );
  await waitFor(() => expect(submitted).toHaveLength(1));
}

// The secret field's own buttons are scoped to the field they belong to,
// because two other Cancel buttons share the name with it: the drawer's
// own Cancel (which closes the card) and the page bar's Cancel (which
// discards pending changes). The scope is the field's Stack container,
// reached from the input's label: label, then the labeled field's root
// div, then the Stack that also holds the field's buttons. No role or
// text query can reach that container directly, so this is node access.
function secretField(input: HTMLElement) {
  // eslint-disable-next-line testing-library/no-node-access -- the field's own button group has no queryable role or text
  const label = document.querySelector(
    `label[for="${input.id}"]`,
  ) as HTMLElement | null;
  return within(label!.closest("div")!.parentElement as HTMLElement);
}

it("shows a stored plugin secret as configured, never as its value", async () => {
  await openProviderCard();

  const input = screen.getByLabelText("API key");
  expect(input).toHaveValue("");
  // A stored credential is replaced through an explicit action: the box is
  // read-only until Replace is pressed.
  expect(input).toBeDisabled();
  expect(screen.getByText(/configured, ends in 1a2b/i)).toBeInTheDocument();
  expect(
    secretField(input).getByRole("button", { name: /replace/i }),
  ).toBeInTheDocument();
});

it("keeps the stored key out of a save that does not touch it", async () => {
  const submitted: FormData[] = [];
  const user = await openProviderCard(submitted);

  // Another field of the same provider changes; the secret stays untouched.
  await user.clear(screen.getByLabelText("Region"));
  await user.type(screen.getByLabelText("Region"), "us");
  // The card's Save stages the drawer's answers into the page form.
  await saveCard(user, submitted);

  expect(submitted[0].get("settings-examplehub-region")).toBe("us");
  expect(submitted[0].get("settings-examplehub-api_key")).toBeNull();
});

it("replaces a stored key only through the Replace action", async () => {
  const submitted: FormData[] = [];
  const user = await openProviderCard(submitted);

  const input = screen.getByLabelText("API key");
  // Typing is impossible until Replace is pressed; Replace unlocks the box.
  await user.click(
    secretField(input).getByRole("button", { name: /replace/i }),
  );
  expect(input).toBeEnabled();
  await user.type(input, "unit-replacement");
  expect(
    screen.getByText(/replaces the stored key when you save/i),
  ).toBeInTheDocument();

  await saveCard(user, submitted);
  expect(submitted[0].get("settings-examplehub-api_key")).toBe(
    "unit-replacement",
  );
});

it("cancel returns a staged replacement to Configured", async () => {
  const submitted: FormData[] = [];
  const user = await openProviderCard(submitted);

  const input = screen.getByLabelText("API key");
  await user.click(
    secretField(input).getByRole("button", { name: /replace/i }),
  );
  await user.type(input, "unit-replacement");
  expect(
    screen.getByText(/replaces the stored key when you save/i),
  ).toBeInTheDocument();

  // Cancel takes the staged replacement back out and locks the box again:
  // the save carries nothing about the secret.
  await user.click(secretField(input).getByRole("button", { name: /cancel/i }));
  expect(input).toBeDisabled();
  expect(screen.getByText(/configured, ends in 1a2b/i)).toBeInTheDocument();
  expect(
    screen.queryByText(/replaces the stored key when you save/i),
  ).not.toBeInTheDocument();

  await saveCard(user, submitted);
  expect(submitted[0].get("settings-examplehub-api_key")).toBeNull();
});

// A replacement made only of spaces is not a credential either: the input
// treats it as blank, the staged answer goes away, and the save submits
// nothing about the secret. Saving the spaces would replace the real key
// with them.
it("treats a spaces-only replacement as blank instead of saving the spaces", async () => {
  const submitted: FormData[] = [];
  const user = await openProviderCard(submitted);
  const input = screen.getByLabelText("API key");

  await user.click(
    secretField(input).getByRole("button", { name: /replace/i }),
  );
  await user.type(input, "   ");

  // Nothing is staged: the field returns to Configured, and neither a
  // replacement nor a clear is said.
  expect(screen.getByText(/configured, ends in 1a2b/i)).toBeInTheDocument();
  expect(
    screen.queryByText(/replaces the stored key when you save/i),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByText(/will be cleared when you save/i),
  ).not.toBeInTheDocument();

  await saveCard(user, submitted);
  expect(submitted[0].get("settings-examplehub-api_key")).toBeNull();
});

it("submits the empty string only from the Clear button", async () => {
  const submitted: FormData[] = [];
  const user = await openProviderCard(submitted);
  const input = screen.getByLabelText("API key");

  // Emptying the input by typing unstages the field: the stored key stands.
  await user.click(
    secretField(input).getByRole("button", { name: /replace/i }),
  );
  await user.type(input, "a-typed-replacement");
  await user.clear(input);
  expect(screen.getByText(/configured, ends in 1a2b/i)).toBeInTheDocument();
  expect(
    screen.queryByText(/will be cleared when you save/i),
  ).not.toBeInTheDocument();

  // The Clear button is what stages the removal, and Undo takes it back out.
  await user.click(secretField(input).getByRole("button", { name: /clear/i }));
  expect(
    screen.getByText(/will be cleared when you save/i),
  ).toBeInTheDocument();
  await user.click(secretField(input).getByRole("button", { name: /undo/i }));
  expect(screen.getByText(/configured, ends in 1a2b/i)).toBeInTheDocument();
  await user.click(secretField(input).getByRole("button", { name: /clear/i }));

  await saveCard(user, submitted);
  expect(submitted[0].get("settings-examplehub-api_key")).toBe("");
});

it("says a replacement replaces the key for a credential that failed to decrypt", async () => {
  const submitted: FormData[] = [];
  const user = await openProviderCard(
    submitted,
    {
      general: { theme: "auto", enabled_providers: ["examplehub"] },
      examplehub: {
        api_key: "",
        region: "eu",
        __secret_fields: {
          api_key: { configured: true, decrypt_failed: true },
        },
      },
    },
    [secretHubProvider],
    /example hub/i,
    /saved value could not be decrypted/i,
  );

  // Nothing staged: the failure is the whole message, and the box is
  // read-only like every stored credential.
  expect(
    screen.getByText(/saved value could not be decrypted/i),
  ).toBeInTheDocument();
  const input = screen.getByLabelText("API key");
  expect(input).toBeDisabled();

  // A staged replacement comes first in the text, so the reader is told
  // their typed key replaces the stored one, not asked to re-enter again.
  await user.click(
    secretField(input).getByRole("button", { name: /replace/i }),
  );
  await user.type(input, "unit-replacement");
  expect(
    screen.getByText(/replaces the stored key when you save/i),
  ).toBeInTheDocument();
  expect(
    screen.queryByText(/saved value could not be decrypted/i),
  ).not.toBeInTheDocument();

  // Cancel goes back to the failed state with its own message.
  await user.click(secretField(input).getByRole("button", { name: /cancel/i }));
  expect(input).toBeDisabled();
  expect(
    screen.getByText(/saved value could not be decrypted/i),
  ).toBeInTheDocument();
});

// A plugin that replaces a built-in shares its settings section, and a
// removed plugin leaves its descriptor map behind. The built-in password
// in that section has no descriptor of its own, so it must keep the plain
// password behavior: enabled, editable, and clearable by emptying the box.
it("a leftover plugin descriptor never changes a built-in password in a shared section", async () => {
  const submitted: FormData[] = [];
  const user = await openProviderCard(
    submitted,
    {
      general: { theme: "auto", enabled_providers: ["legendasdivx"] },
      legendasdivx: {
        username: "unit-user",
        password: "unit-pass",
        __secret_fields: {
          captcha_response: { configured: true, last_four: "9ade" },
        },
      },
    },
    [],
    /legendasdivx/i,
    "",
  );

  // The card's fields are queried inside the drawer, because the page
  // behind it carries the anti-captcha card's own Username and Password.
  const dialog = await screen.findByRole("dialog");
  const input = within(dialog).getByLabelText("Password");
  // A built-in password reads its value back like it always has; the store
  // has to land first.
  await waitFor(() => expect(input).toHaveValue("unit-pass"));
  expect(input).toBeEnabled();
  // The plain input has no replacement states of its own.
  expect(
    within(dialog).queryByRole("button", { name: /replace/i }),
  ).not.toBeInTheDocument();
  expect(
    within(dialog).queryByRole("button", { name: /clear/i }),
  ).not.toBeInTheDocument();

  // Emptying the box stages the empty value, the built-in's own clear.
  await user.clear(input);
  expect(input).toHaveValue("");

  await saveCard(user, submitted);
  expect(submitted[0].get("settings-legendasdivx-password")).toBe("");
});
