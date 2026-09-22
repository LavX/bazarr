import { FC, ReactNode } from "react";
import { Text, Title } from "@mantine/core";
import styles from "./StepLayout.module.scss";

/**
 * How a step uses the width it is given.
 *
 * `split` is for a step that has something to say beside its controls: an
 * explanation plus a live status or result, next to a form. `stacked` is for a
 * step whose explanation is a sentence or two, where a side column would be a
 * sliver beside a void; it keeps one column at a comfortable reading measure.
 * `wide` is for a step whose body is already a grid and wants the whole shell.
 */
export type StepLayoutVariant = "split" | "stacked" | "wide";

interface Props {
  /** The step heading. Level 2, except inside the media server segment. */
  title: ReactNode;
  titleOrder?: 2 | 3;
  /** The paragraph under the heading. */
  description?: ReactNode;
  /**
   * Anything else that explains or reports rather than accepts input: a note,
   * a live status, a result the reader reads and does not type into. In a
   * split step it sits with the explanation, not with the controls.
   */
  aside?: ReactNode;
  /** The Back and Continue row, always full width under everything else. */
  actions?: ReactNode;
  layout?: StepLayoutVariant;
  /** The controls. Omit for a step that only explains. */
  children?: ReactNode;
}

/**
 * The frame every wizard step is laid out in.
 *
 * One screen, one step, no scrolling: the wizard has to fit the display it is
 * given, and the tallest screens ran nearly a thousand pixels past the bottom
 * of a 1080 monitor while leaving six hundred pixels unused on each side. A
 * reading measure is the wrong instrument for a configuration surface, so the
 * shell is wide and a `split` step puts its explanation and status on the left
 * and its controls on the right from 1200px up. Below that width, and in the
 * other two variants, it is one column in the same order.
 *
 * Two columns is not a rule for every step. A step with one sentence to its
 * name has nothing to put in a side column, so it stays `stacked` and reads
 * down the middle instead.
 *
 * The actions row always spans the full width, so Back and Continue stay in
 * one place whatever the step above them does.
 */
const StepLayout: FC<Props> = ({
  title,
  titleOrder = 2,
  description,
  aside,
  actions,
  layout = "split",
  children,
}) => (
  <div
    className={
      layout === "wide"
        ? styles.wide
        : layout === "stacked" || !children
          ? styles.stacked
          : styles.layout
    }
  >
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
