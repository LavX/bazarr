import { FunctionComponent, PropsWithChildren } from "react";
import { Group, GroupProps } from "@mantine/core";
import clsx from "clsx";
import ToolboxButton, { ToolboxMutateButton } from "./Button";
import styles from "./Toolbox.module.scss";

type ToolboxProps = PropsWithChildren<
  Pick<GroupProps, "className"> & {
    // Lets a caller key its own layout to the band's state, as ItemView does
    // with what its left side is holding, without the band knowing why.
    [data: `data-${string}`]: string | undefined;
  }
>;

declare type ToolboxComp = FunctionComponent<ToolboxProps> & {
  Button: typeof ToolboxButton;
  MutateButton: typeof ToolboxMutateButton;
};

const Toolbox: ToolboxComp = ({ children, className, ...data }) => {
  return (
    /* 12px all round put roughly 40px of chrome around a single 36px input,
       which on a table whose left half is empty until something is selected
       read as a band of nothing. The horizontal inset is what separates the
       controls from the table edges; the vertical is not carrying anything. */
    <Group
      py={6}
      px={12}
      justify="space-between"
      className={clsx(styles.group, className)}
      {...data}
    >
      {children}
    </Group>
  );
};

Toolbox.Button = ToolboxButton;
Toolbox.MutateButton = ToolboxMutateButton;

export default Toolbox;
