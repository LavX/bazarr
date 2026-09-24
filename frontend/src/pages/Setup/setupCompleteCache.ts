import type { QueryClient } from "@tanstack/react-query";
import { QueryKeys } from "@/apis/queries/keys";
import { clearPersistedIntent } from "./useOnboardingIntent";
import { clearPersistedSelection } from "./useOnboardingSelection";
import { clearPersistedStep } from "./useWizardStep";

/**
 * Makes the cached settings agree with a setup_complete value the server has
 * just saved, and waits for the fresh read before the caller navigates.
 *
 * The settings mutation only invalidates, so the next page to mount still sees
 * the old value until the refetch returns. Leaving setup navigates to "/", and
 * the Redirector there read setup_complete as false and sent the reader
 * straight back into the wizard they had just left.
 */
export async function settleSetupComplete(
  client: QueryClient,
  complete: boolean,
) {
  const queryKey = [QueryKeys.System, QueryKeys.Settings];
  client.setQueryData<Settings>(queryKey, (current) =>
    current === undefined
      ? current
      : {
          ...current,
          general: { ...current.general, setup_complete: complete },
        },
  );
  await client.invalidateQueries({ queryKey });
}

/** Forgets the wizard's stored step, intent and media server drafts. */
export function clearPersistedOnboarding() {
  clearPersistedStep();
  clearPersistedIntent();
  clearPersistedSelection();
}
