import {
  FormEvent,
  FunctionComponent,
  ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  Alert,
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
import {
  isMetadataFollowupError,
  useSettingsMutation,
  useSystemSettings,
} from "@/apis/hooks";
import { useInstanceName } from "@/apis/hooks/site";
import { LoadingProvider } from "@/contexts";
import {
  FormContext,
  FormValues,
  runHooks,
} from "@/pages/Settings/utilities/FormValues";
import { settingsLogValues } from "@/pages/Settings/utilities/settingsLog";
import { SettingsProvider } from "@/pages/Settings/utilities/SettingsProvider";
import { useOnValueChange } from "@/utilities";
import { LOG } from "@/utilities/console";
import { usePrompt } from "@/utilities/routers";

interface Props {
  name: string;
  children: ReactNode;
  fluid?: boolean;
}

const Layout: FunctionComponent<Props> = (props) => {
  const { children, fluid = false, name } = props;

  const { data: settings, isLoading, isRefetching } = useSystemSettings();
  const [metadataRefreshFailed, setMetadataRefreshFailed] = useState(false);
  const reducedMotion = useReducedMotion();

  const form = useForm<FormValues>({
    initialValues: {
      settings: {},
      hooks: {},
    },
  });

  const formRef = useRef(form);
  formRef.current = form;
  const totalStagedCount = Object.keys(form.values.settings).length;
  const {
    mutate,
    mutateAsync,
    isPending: isMutating,
  } = useSettingsMutation(metadataRefreshFailed && totalStagedCount === 0);
  const handleSaveError = useCallback((error: unknown) => {
    if (isMetadataFollowupError(error)) {
      formRef.current.reset();
      setMetadataRefreshFailed(true);
    }
  }, []);

  useOnValueChange(isRefetching, (value) => {
    if (
      !value &&
      !Object.keys(form.values.settings).some((key) =>
        key.startsWith("settings-discover-"),
      )
    ) {
      form.reset();
    }
  });

  const submit = useCallback(
    (values: FormValues) => {
      const { settings, hooks } = values;
      if (Object.keys(settings).length > 0 || metadataRefreshFailed) {
        const settingsToSubmit = { ...settings };
        runHooks(hooks, settingsToSubmit);
        LOG("info", "submitting settings", settingsLogValues(settingsToSubmit));
        mutate(settingsToSubmit, {
          onSuccess: () => {
            setMetadataRefreshFailed(false);
            if (
              Object.keys(settingsToSubmit).some((key) =>
                key.startsWith("settings-discover-"),
              )
            )
              formRef.current.reset();
          },
          onError: handleSaveError,
        });
      }
    },
    [mutate, metadataRefreshFailed, handleSaveError],
  );

  const submitAndLeave = useCallback(async () => {
    const { settings, hooks } = form.values;
    if (Object.keys(settings).length > 0 || metadataRefreshFailed) {
      const settingsToSubmit = { ...settings };
      runHooks(hooks, settingsToSubmit);
      LOG("info", "save & leave", settingsLogValues(settingsToSubmit));
      try {
        await mutateAsync(settingsToSubmit);
        setMetadataRefreshFailed(false);
      } catch (error) {
        handleSaveError(error);
        throw error;
      }
    }
  }, [form.values, mutateAsync, metadataRefreshFailed, handleSaveError]);

  usePrompt(
    totalStagedCount > 0 || metadataRefreshFailed,
    metadataRefreshFailed
      ? `Your previous settings were saved, but application refresh failed. Leaving keeps those saved settings.${totalStagedCount > 0 ? ` You also have ${totalStagedCount} new unsaved change${totalStagedCount !== 1 ? "s" : ""}, which will be discarded if you leave without saving.` : ""}`
      : `You have ${totalStagedCount} unsaved change${totalStagedCount !== 1 ? "s" : ""}. What would you like to do?`,
    submitAndLeave,
    metadataRefreshFailed,
  );

  useDocumentTitle(`${name} - ${useInstanceName()} (Settings)`);

  // Some inputs only stage their value when they are left, so the focused field
  // is blurred first and the submit waits a tick for that change to land.
  // Without it a save keeps the value the field held before the user's last
  // edit. Every route into a save goes through here: the Save button, Enter in
  // a field, and the keyboard shortcut below.
  const commitAndSubmit = useCallback(() => {
    const focused = document.activeElement;
    if (focused instanceof HTMLElement) {
      focused.blur();
    }

    // formRef, not form: a blur handler restages values, and the closure this
    // was created in would otherwise submit the ones captured before it ran.
    window.setTimeout(() => formRef.current.onSubmit(submit)(), 0);
  }, [submit]);

  const onFormSubmit = useCallback(
    (e: FormEvent<HTMLFormElement>) => {
      e.preventDefault();
      commitAndSubmit();
    },
    [commitAndSubmit],
  );

  // Ctrl+S / Cmd+S keyboard shortcut
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "s") {
        e.preventDefault();
        if (totalStagedCount > 0 || metadataRefreshFailed) {
          commitAndSubmit();
        }
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [commitAndSubmit, totalStagedCount, metadataRefreshFailed]);

  return (
    <SettingsProvider value={settings ?? null}>
      <LoadingProvider value={isLoading || isMutating}>
        <form onSubmit={onFormSubmit} style={{ position: "relative" }}>
          <LoadingOverlay visible={settings === undefined} />
          <FormContext.Provider value={form}>
            <Container
              data-testid="settings-layout-content"
              fluid={fluid}
              size="xl"
              mx={0}
              pb={80}
              style={
                fluid
                  ? {
                      maxWidth: "none",
                      width: "100%",
                    }
                  : undefined
              }
            >
              {metadataRefreshFailed && (
                <Alert color="yellow" mb="md">
                  Your previous settings were saved, but application refresh
                  failed. Leaving keeps the saved settings.
                </Alert>
              )}
              {children}
            </Container>
          </FormContext.Provider>
          {/* Floating save, sticky bottom, after form fields in DOM for correct tab order */}
          <Transition
            transition={reducedMotion ? "fade" : "slide-up"}
            mounted={totalStagedCount > 0 || metadataRefreshFailed}
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
                    aria-label={
                      metadataRefreshFailed && totalStagedCount === 0
                        ? "Retry application refresh"
                        : `Save ${totalStagedCount} pending change${totalStagedCount !== 1 ? "s" : ""}`
                    }
                    style={{ boxShadow: "var(--bz-shadow-float)" }}
                  >
                    {metadataRefreshFailed && totalStagedCount === 0
                      ? "Retry refresh"
                      : "Save"}
                    {totalStagedCount > 0 && (
                      <Badge
                        size="sm"
                        radius="xl"
                        ml={8}
                        aria-label={`${totalStagedCount} unsaved change${totalStagedCount !== 1 ? "s" : ""}`}
                        variant="filled"
                        style={{
                          minWidth: 22,
                          height: 22,
                          paddingInline: 7,
                          background: "var(--mantine-color-white)",
                          color: "var(--mantine-color-brand-7)",
                          fontWeight: 800,
                          lineHeight: "22px",
                          boxShadow: "0 0 0 1px rgba(255, 255, 255, 0.42)",
                        }}
                      >
                        {totalStagedCount}
                      </Badge>
                    )}
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
