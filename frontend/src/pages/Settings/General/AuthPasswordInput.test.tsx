import { useForm } from "@mantine/form";
import userEvent from "@testing-library/user-event";
import { expect, it } from "vitest";
import { FormContext, FormValues } from "@/pages/Settings/utilities/FormValues";
import { customRender, screen } from "@/tests";
import { AuthPasswordInput } from "./index";

function Harness() {
  const form = useForm<FormValues>({
    initialValues: { settings: {}, hooks: {} },
  });
  return (
    <FormContext.Provider value={form}>
      <AuthPasswordInput />
      <output data-testid="staged">
        {JSON.stringify(Object.keys(form.values.settings))}
      </output>
    </FormContext.Provider>
  );
}

it("stages a typed password and takes it back out when the field is cleared", async () => {
  const user = userEvent.setup();
  customRender(<Harness />);
  const field = screen.getByLabelText("Password");

  await user.type(field, "abc");
  expect(screen.getByTestId("staged")).toHaveTextContent(
    '["settings-auth-password"]',
  );

  // Cleared key by key: a no-op edit stages nothing, so a save has nothing to
  // send and no password to ask for.
  await user.clear(field);
  expect(screen.getByTestId("staged")).toHaveTextContent("[]");

  await user.type(field, "x");
  expect(screen.getByTestId("staged")).toHaveTextContent(
    '["settings-auth-password"]',
  );
});
