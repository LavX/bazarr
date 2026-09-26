import { FunctionComponent } from "react";
import { Text } from "@mantine/core";
import styles from "./ScorePill.module.scss";

interface Props {
  score?: number | null;
}

// A rounded pill for an item's lowest current-subtitle score, colour-banded the
// same way as the detail tables: green from 90%, yellow from 70%, red below. A
// null score (no current subtitle with a known score) shows a dim dash instead.
const ScorePill: FunctionComponent<Props> = ({ score }) => {
  if (score === undefined || score === null) {
    return (
      <Text c="dimmed" size="sm">
        —
      </Text>
    );
  }
  const band = score >= 90 ? "green" : score >= 70 ? "yellow" : "red";
  return (
    <span className={`${styles.pill} ${styles[band]}`}>
      {`${Math.round(score)}%`}
    </span>
  );
};

export default ScorePill;
