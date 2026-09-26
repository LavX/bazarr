import { useState } from "react";
import { faFilm } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import styles from "./Discover.module.scss";

/**
 * One poster treatment for every discovery feed.
 *
 * The slot has three states and says something true in each. With no source,
 * or a source that failed, it says artwork is unavailable. With a source that
 * has not painted yet, it shows the same surface and the same icon with no
 * label, because a lazy image that has not reached the viewport is pending,
 * not missing, and a slot that says "unavailable" over a poster that exists is
 * a lie the reader meets every time they scroll quickly. Once the image paints
 * it covers the slot and the treatment is removed.
 *
 * Callers pass the poster URL as `key` so a changed source restarts the load
 * instead of inheriting the previous tile's failure.
 */
export default function MediaPoster({ src }: { src: string | null }) {
  const [failed, setFailed] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const missing = !src || failed;
  return (
    <>
      {/* Not `hidden`: an author `display: flex` on this class beats the user
          agent's [hidden] rule, so the attribute would say something it does
          not do. The class actually removes it once the image has painted. */}
      <span
        className={
          loaded
            ? styles.missingArtHidden
            : missing
              ? styles.missingArt
              : styles.artPending
        }
        aria-hidden="true"
      >
        <FontAwesomeIcon icon={faFilm} />
        {missing && <span>Artwork unavailable</span>}
      </span>
      {src && !failed && (
        <img
          src={src}
          alt=""
          loading="lazy"
          decoding="async"
          className={loaded ? undefined : styles.pendingArt}
          onLoad={() => setLoaded(true)}
          onError={() => setFailed(true)}
        />
      )}
    </>
  );
}

/**
 * The feature backdrop is decoration: when it is missing there is nothing to
 * stand in for, so it leaves no gap behind. Posters are the opposite, and
 * MediaPoster above owns that treatment for every feed.
 */
export function Backdrop({ src }: { src: string | null }) {
  const [failed, setFailed] = useState(false);
  if (!src || failed) return null;
  return (
    <img
      src={src}
      alt=""
      loading="eager"
      decoding="async"
      onError={() => setFailed(true)}
    />
  );
}
