import type { ArrMediaDefaults } from "@/apis/raw/arrInstances";

/**
 * The per-instance default language profile has exactly three states, and the
 * form encodes them as one Select value so "use the global default" is an
 * explicit choice rather than a blank field the user has to interpret.
 *
 * The mapping is deliberately total in both directions: an absent block must
 * read back as GLOBAL_DEFAULT and write back as an empty block. Anything that
 * turned "nothing stored" into a picked profile would start reassigning media
 * on installs that never asked for an override.
 */
export const GLOBAL_DEFAULT = "global";
export const NO_PROFILE = "none";

export function mediaDefaultsToValue(
  blob: ArrMediaDefaults | undefined,
): string {
  if (!blob || blob.default_enabled === undefined) {
    return GLOBAL_DEFAULT;
  }
  if (blob.default_enabled !== true) {
    return NO_PROFILE;
  }
  return blob.default_profile === undefined || blob.default_profile === null
    ? NO_PROFILE
    : String(blob.default_profile);
}

export function valueToMediaDefaults(value: string): ArrMediaDefaults {
  if (value === GLOBAL_DEFAULT) {
    return {};
  }
  if (value === NO_PROFILE) {
    return { default_enabled: false };
  }
  return { default_enabled: true, default_profile: Number(value) };
}

// Whether the instance actually assigns a profile, which is the only case the
// "apply to media without a profile" action has anything to do.
export function hasMediaDefaultProfile(
  blob: ArrMediaDefaults | undefined,
): boolean {
  return (
    blob?.default_enabled === true && typeof blob.default_profile === "number"
  );
}
