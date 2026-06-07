import { useCallback, useEffect, useRef } from "react";
import { WhatsNewModal } from "@/components/modals/WhatsNewModal";
import { getWhatsNewSlides, latestWhatsNewVersion } from "@/data/whatsNew";
import { useModals } from "@/modules/modals";
import {
  getSeenWhatsNewVersion,
  markWhatsNewSeen,
  shouldAutoOpenWhatsNew,
} from "@/utilities/whatsNew";

/** Whether there is any "What's New" content for the current build. */
export function hasWhatsNew(): boolean {
  return getWhatsNewSlides(latestWhatsNewVersion).length > 0;
}

/** Returns a callback that opens the wizard for the latest version on demand. */
export function useOpenWhatsNew() {
  const { openContextModal } = useModals();
  return useCallback(() => {
    const slides = getWhatsNewSlides(latestWhatsNewVersion);
    if (slides.length === 0) {
      return;
    }
    openContextModal(WhatsNewModal, {
      version: latestWhatsNewVersion,
      slides,
    });
  }, [openContextModal]);
}

/**
 * Auto-opens the wizard once when the stored "seen" token is missing or older than the
 * latest version. Marks the version seen at open time, which also keeps React StrictMode's
 * double-invoke from stacking two modals. Users can re-open later from System > Status.
 */
/**
 * Auto-opens the wizard once after upgrading. `enabled` should be true only once the app
 * is authenticated and loaded (e.g. settings have loaded) so the wizard never appears over
 * the login screen. Marks the version seen at open time so it shows at most once.
 */
export function useWhatsNewAutoOpen(enabled: boolean) {
  const { openContextModal } = useModals();
  const opened = useRef(false);
  useEffect(() => {
    if (!enabled || opened.current) {
      return;
    }
    const slides = getWhatsNewSlides(latestWhatsNewVersion);
    if (
      !shouldAutoOpenWhatsNew(
        getSeenWhatsNewVersion(),
        latestWhatsNewVersion,
        slides.length,
      )
    ) {
      opened.current = true;
      return;
    }
    opened.current = true;
    markWhatsNewSeen(latestWhatsNewVersion);
    openContextModal(WhatsNewModal, {
      version: latestWhatsNewVersion,
      slides,
    });
  }, [enabled, openContextModal]);
}
