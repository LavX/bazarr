import { FunctionComponent, PropsWithChildren } from "react";
import { Group } from "@mantine/core";
import ToolboxButton, { ToolboxMutateButton } from "./Button";
import styles from "./Toolbox.module.scss";

declare type ToolboxComp = FunctionComponent<PropsWithChildren> & {
  Button: typeof ToolboxButton;
  MutateButton: typeof ToolboxMutateButton;
};

const Toolbox: ToolboxComp = ({ children }) => {
  return (
    /* 12px all round put roughly 40px of chrome around a single 36px input,
       which on a table whose left half is empty until something is selected
       read as a band of nothing. The horizontal inset is what separates the
       controls from the table edges; the vertical is not carrying anything. */
    <Group py={6} px={12} justify="space-between" className={styles.group}>
      {children}
    </Group>
  );
};

Toolbox.Button = ToolboxButton;
Toolbox.MutateButton = ToolboxMutateButton;

export default Toolbox;
