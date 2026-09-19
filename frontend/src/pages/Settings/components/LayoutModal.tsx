import {
  FunctionComponent,
  ReactNode,
  useCallback,
  useMemo,
  useRef,
} from "react";
import {
  Button,
  Container,
  Divider,
  Group,
  LoadingOverlay,
  Space,
} from "@mantine/core";
import { useForm } from "@mantine/form";
import { useSettingsMutation, useSystemSettings } from "@/apis/hooks";
import { LoadingProvider } from "@/contexts";
import {
  FormContext,
  FormValues,
  runHooks,
} from "@/pages/Settings/utilities/FormValues";
import { SettingsProvider } from "@/pages/Settings/utilities/SettingsProvider";
import { useOnValueChange } from "@/utilities";
import { LOG } from "@/utilities/console";

interface Props {
  children: ReactNode;
  callbackModal: (value: boolean) => void;
}

const LayoutModal: FunctionComponent<Props> = (props) => {
  const { children, callbackModal } = props;

  const { data: settings, isLoading, isRefetching } = useSystemSettings();
  const { mutate, isPending: isMutating } = useSettingsMutation();

  const form = useForm<FormValues>({
    initialValues: {
      settings: {},
      hooks: {},
    },
  });

  // Same rule as the settings page: only the reload that follows a save may
  // clear the form. A startup or reconnect refresh must leave staged values
  // alone, or an edit made while the response is in flight is lost silently.
  const savedRef = useRef(false);

  useOnValueChange(isRefetching, (value) => {
    if (!value && savedRef.current) {
      savedRef.current = false;
      form.reset();
    }
  });

  const submit = useCallback(
    (values: FormValues) => {
      const { settings, hooks } = values;
      if (Object.keys(settings).length > 0) {
        const settingsToSubmit = { ...settings };
        runHooks(hooks, settingsToSubmit);
        LOG("info", "submitting settings", Object.keys(settingsToSubmit));
        mutate(settingsToSubmit, {
          onSuccess: () => {
            savedRef.current = true;
          },
        });
        // wait for settings to be validated before callback
        // let the user see the spinning indicator on the Save button before the modal closes
        setTimeout(() => {
          callbackModal(true);
        }, 500);
      }
    },
    [mutate, callbackModal],
  );

  const totalStagedCount = useMemo(() => {
    return Object.keys(form.values.settings).length;
  }, [form.values.settings]);

  return (
    <SettingsProvider value={settings ?? null}>
      <LoadingProvider value={isLoading || isMutating}>
        <form onSubmit={form.onSubmit(submit)} style={{ position: "relative" }}>
          <LoadingOverlay visible={settings === undefined}></LoadingOverlay>
          <FormContext.Provider value={form}>
            <Container size="xl" mx={0}>
              {children}
            </Container>
          </FormContext.Provider>
          <Space h="md" />
          <Divider></Divider>
          <Space h="md" />
          <Group justify="right">
            <Button
              type="submit"
              disabled={totalStagedCount === 0}
              loading={isMutating}
            >
              Save
            </Button>
            <Button
              onClick={() => {
                callbackModal(true);
              }}
            >
              Close
            </Button>
          </Group>
        </form>
      </LoadingProvider>
    </SettingsProvider>
  );
};

export default LayoutModal;
