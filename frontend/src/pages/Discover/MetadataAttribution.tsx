import { Anchor, Text } from "@mantine/core";
import tmdbLogo from "@/assets/images/tmdb-blue-short.svg";
import styles from "./Discover.module.scss";

export default function MetadataAttribution() {
  return (
    <div className={styles.attribution}>
      <Anchor
        href="https://www.themoviedb.org/"
        target="_blank"
        rel="noopener noreferrer"
        aria-label="TMDB, metadata source"
      >
        <img src={tmdbLogo} width={110} height={14.3} alt="TMDB" />
      </Anchor>
      <Text size="xs">
        This product uses the TMDB API but is not endorsed or certified by TMDB.
      </Text>
    </div>
  );
}
