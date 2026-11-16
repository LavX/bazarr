import { CSSProperties, FunctionComponent, PropsWithChildren } from "react";
import styles from "./Translation.module.scss";

/**
 * Why an engine or a back end was turned off without the person asking, and what to do about it. It
 * stands where the eye already is: under an engine's name, above the back-end list, at the top of a
 * back end's form. It is part of the page as loaded, so it is not announced like an alert.
 */
const AttentionNotice: FunctionComponent<
  PropsWithChildren<{ title: string; style?: CSSProperties }>
> = ({ title, style, children }) => (
  <div className={styles.attention} style={style}>
    <p className={styles.conflictTitle}>{title}</p>
    <p className={styles.attentionText}>{children}</p>
  </div>
);

export default AttentionNotice;
