import { useForm } from "@mantine/form";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";
import { customRender, screen } from "@/tests";
import {
  FormContext,
  FormValues,
  runHooks,
  useFormActions,
} from "./FormValues";

function StageSecrets() {
  const { update, setValue } = useFormActions();
  return (
    <button
      onClick={() => {
        update({ "settings-silo-apikey": "sentinel-stage-secret" });
        setValue(
          "sentinel-stage-secret",
          "settings-emby-apikey",
          (value: string) => `${value}-hooked`,
        );
      }}
    >
      Stage secrets
    </button>
  );
}

function Harness({ saved }: { saved: LooseObject[] }) {
  const form = useForm<FormValues>({
    initialValues: { settings: {}, hooks: {} },
  });
  return (
    <FormContext.Provider value={form}>
      <StageSecrets />
      <button
        onClick={() => {
          const values = { ...form.values.settings };
          runHooks(form.values.hooks, values);
          saved.push(values);
        }}
      >
        Run hooks
      </button>
    </FormContext.Provider>
  );
}

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

it("logs staged field names with the real development logger, never values before or after hooks", async () => {
  vi.stubEnv("MODE", "development");
  const log = vi.spyOn(console, "log").mockImplementation(() => undefined);
  const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
  const error = vi.spyOn(console, "error").mockImplementation(() => undefined);
  const saved: LooseObject[] = [];
  customRender(<Harness saved={saved} />);
  await userEvent.click(screen.getByRole("button", { name: "Stage secrets" }));
  await userEvent.click(screen.getByRole("button", { name: "Run hooks" }));
  expect(log).toHaveBeenCalledWith(
    "[info] Updating value of settings-emby-apikey",
  );
  expect(log).toHaveBeenCalledWith("[info] Updating values", [
    "settings-silo-apikey",
  ]);
  expect(log).toHaveBeenCalledWith(
    "[info] Running submit hook for",
    "settings-emby-apikey",
  );
  expect(log).toHaveBeenCalledWith(
    "[info] Finish submit hook",
    "settings-emby-apikey",
  );
  expect(
    JSON.stringify([log.mock.calls, warn.mock.calls, error.mock.calls]),
  ).not.toContain("sentinel-stage-secret");
  expect(saved).toEqual([
    {
      "settings-silo-apikey": "sentinel-stage-secret",
      "settings-emby-apikey": "sentinel-stage-secret-hooked",
    },
  ]);
});
