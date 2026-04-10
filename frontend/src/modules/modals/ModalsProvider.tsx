import { FunctionComponent, PropsWithChildren, useMemo } from "react";
import {
  ModalsProvider as MantineModalsProvider,
  ModalsProviderProps as MantineModalsProviderProps,
} from "@mantine/modals";
import { ModalComponent, StaticModals } from "./WithModal";

const DefaultModalProps: MantineModalsProviderProps["modalProps"] = {
  centered: true,
  styles: {
    content: {
      background: "var(--bz-surface-overlay)",
      border: "1px solid var(--bz-border-card)",
      borderRadius: "var(--bz-radius-lg)",
    },
    header: {
      background: "transparent",
    },
    body: {
      background: "transparent",
    },
    overlay: {
      background: "rgba(0, 0, 0, 0.6)",
    },
  },
};

const ModalsProvider: FunctionComponent<PropsWithChildren> = ({ children }) => {
  const modals = useMemo(
    () =>
      StaticModals.reduce<Record<string, ModalComponent>>((prev, curr) => {
        prev[curr.modalKey] = curr;
        return prev;
      }, {}),
    [],
  );

  return (
    <MantineModalsProvider modalProps={DefaultModalProps} modals={modals}>
      {children}
    </MantineModalsProvider>
  );
};

export default ModalsProvider;
