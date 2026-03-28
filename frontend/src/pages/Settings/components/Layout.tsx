import { FunctionComponent, ReactNode, useCallback, useEffect, useMemo } from "react";
import {
  Badge,
  Box,
  Button,
  Container,
  Group,
  LoadingOverlay,
  Transition,
} from "@mantine/core";
import { useForm } from "@mantine/form";
import { useDocumentTitle, useReducedMotion } from "@mantine/hooks";
import { faFloppyDisk } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useSettingsMutation, useSystemSettings } from "@/apis/hooks";
import { useInstanceName } from "@/apis/hooks/site";
import { LoadingProvider } from "@/contexts";
import {
  FormContext,
  FormValues,
  runHooks,
} from "@/pages/Settings/utilities/FormValues";
import { SettingsProvider } from "@/pages/Settings/utilities/SettingsProvider";
import { useOnValueChange } from "@/utilities";
import { LOG } from "@/utilities/console";
import { usePrompt } from "@/utilities/routers";

interface Props {
  name: string;
  children: ReactNode;
}

const Layout: FunctionComponent<Props> = (props) => {
  const { children, name } = props;

  const { data: settings, isLoading, isRefetching } = useSystemSettings();
  const { mutate, mutateAsync, isPending: isMutating } = useSettingsMutation();
  const reducedMotion = useReducedMotion();

  const form = useForm<FormValues>({
    initialValues: {
      settings: {},
      hooks: {},
    },
  });

  useOnValueChange(isRefetching, (value) => {
    if (!value) {
      form.reset();
    }
  });

  const submit = useCallback(
    (values: FormValues) => {
      const { settings, hooks } = values;
      if (Object.keys(settings).length > 0) {
        const settingsToSubmit = { ...settings };
        runHooks(hooks, settingsToSubmit);
        LOG("info", "submitting settings", settingsToSubmit);
        mutate(settingsToSubmit);
      }
    },
    [mutate],
  );

  const submitAndLeave = useCallback(async () => {
    const { settings, hooks } = form.values;
    if (Object.keys(settings).length > 0) {
      const settingsToSubmit = { ...settings };
      runHooks(hooks, settingsToSubmit);
      LOG("info", "save & leave", settingsToSubmit);
      await mutateAsync(settingsToSubmit);
    }
  }, [form.values, mutateAsync]);

  const totalStagedCount = useMemo(() => {
    return Object.keys(form.values.settings).length;
  }, [form.values.settings]);

  usePrompt(
    totalStagedCount > 0,
    `You have ${totalStagedCount} unsaved change${totalStagedCount !== 1 ? "s" : ""}. What would you like to do?`,
    submitAndLeave,
  );

  useDocumentTitle(`${name} - ${useInstanceName()} (Settings)`);

  // Ctrl+S / Cmd+S keyboard shortcut
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "s") {
        e.preventDefault();
        if (totalStagedCount > 0) {
          form.onSubmit(submit)();
        }
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [form, submit, totalStagedCount]);

  return (
    <SettingsProvider value={settings ?? null}>
      <LoadingProvider value={isLoading || isMutating}>
        <form
          onSubmit={form.onSubmit(submit)}
          style={{ position: "relative" }}
        >
          <LoadingOverlay visible={settings === undefined} />
          <FormContext.Provider value={form}>
            <Container size="xl" mx={0} pb={80}>
              {children}
            </Container>
          </FormContext.Provider>
          {/* Floating save — sticky bottom, after form fields in DOM for correct tab order */}
          <Transition
            transition={reducedMotion ? "fade" : "slide-up"}
            mounted={totalStagedCount > 0}
          >
            {(styles) => (
              <Box
                pos="sticky"
                bottom={16}
                style={{
                  ...styles,
                  zIndex: 100,
                  display: "flex",
                  justifyContent: "flex-end",
                  paddingRight: 24,
                  pointerEvents: "none",
                }}
              >
                <Group justify="flex-end" style={{ pointerEvents: "auto" }}>
                  <div
                    aria-live="polite"
                    role="status"
                    style={{
                      position: "absolute",
                      width: 1,
                      height: 1,
                      overflow: "hidden",
                      clip: "rect(0,0,0,0)",
                    }}
                  >
                    {totalStagedCount > 0
                      ? `You have ${totalStagedCount} unsaved change${totalStagedCount !== 1 ? "s" : ""}`
                      : ""}
                  </div>
                  <Button
                    type="submit"
                    radius="xl"
                    variant="gradient"
                    gradient={{ from: "brand.5", to: "brand.6", deg: 135 }}
                    size="md"
                    leftSection={<FontAwesomeIcon icon={faFloppyDisk} />}
                    loading={isMutating}
                    aria-label={`Save ${totalStagedCount} pending change${totalStagedCount !== 1 ? "s" : ""}`}
                    style={{ boxShadow: "var(--bz-shadow-float)" }}
                  >
                    Save
                    <Badge size="sm" circle ml={8} aria-hidden="true" variant="outline" style={{ color: "inherit", borderColor: "currentColor", opacity: 0.7 }}>
                      {totalStagedCount}
                    </Badge>
                  </Button>
                </Group>
              </Box>
            )}
          </Transition>
        </form>
      </LoadingProvider>
    </SettingsProvider>
  );
};

export default Layout;
