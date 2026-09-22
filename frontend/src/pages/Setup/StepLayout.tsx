import { FC, ReactNode } from "react";
import { Text, Title } from "@mantine/core";
import styles from "./StepLayout.module.scss";

interface Props {
  /** The step heading. Level 2, except inside the media server segment. */
  title: ReactNode;
  titleOrder?: 2 | 3;
  /** The paragraph under the heading. */
  description?: ReactNode;
  /**
   * Anything else that explains or reports rather than accepts input: a note,
   * a live status, a result the reader reads and does not type into. It sits
   * with the explanation, not with the controls.
   */
  aside?: ReactNode;
  /** The Back and Continue row, always full width under both columns. */
  actions?: ReactNode;
  /** The controls. Omit for a step that only explains. */
  children?: ReactNode;
}

/**
 * The frame every wizard step is laid out in.
 *
 * One screen, one step, no scrolling: the wizard has to fit the display it is
 * given, and the tallest screens ran nearly a thousand pixels past the bottom
 * of a 1080 monitor while leaving six hundred pixels unused on each side. A
 * reading measure is the wrong instrument for a configuration surface. From
 * 1200px up the step is two columns, the explanation and whatever the step is
 * reporting on the left and the controls on the right, so a form that used to
 * be one tall column is half as tall. Below that width it is the single column
 * it always was, in the same order.
 *
 * The actions row spans both columns, so Back and Continue stay in one place
 * whatever the step above them does.
 */
const StepLayout: FC<Props> = ({
  title,
  titleOrder = 2,
  description,
  aside,
  actions,
  children,
}) => (
  <div className={children ? styles.layout : styles.single}>
    <div className={styles.intro}>
      <Title order={titleOrder}>{title}</Title>
      {description && <Text c="dimmed">{description}</Text>}
      {aside}
    </div>
    {children && <div className={styles.controls}>{children}</div>}
    {actions && <div className={styles.actions}>{actions}</div>}
  </div>
);

export default StepLayout;
