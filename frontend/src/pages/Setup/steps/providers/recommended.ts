import type { ProviderHubManifest } from "@/apis/raw/providerHub";
import {
  usesAntiCaptcha,
  usesFlaresolverr,
} from "@/pages/Settings/Providers/hub/utils";
import { detectAuthFromManifest } from "@/pages/Settings/Providers/meta";

export interface RecommendedCandidate {
  providerId: string;
  manifest: ProviderHubManifest;
}

function requiredConfigKeys(manifest: ProviderHubManifest): string[] {
  const schema = (manifest as LooseObject)?.config_schema as
    | LooseObject
    | undefined;
  const required = schema?.required;
  if (!Array.isArray(required)) {
    return [];
  }
  return required.filter((key): key is string => typeof key === "string");
}

/**
 * Whether a catalog provider belongs in the first-run "install recommended"
 * set: one that works on a fresh install with nothing asked of the reader.
 *
 * Three conditions, all read off the catalog manifest so the set stays right as
 * the catalog changes and no list here has to be kept in step with it:
 *
 * 1. No signup. detectAuthFromManifest is the same read the Subtitle Hub uses
 *    for its "No signup" badge, so the wizard and the hub never disagree about
 *    what a provider asks for.
 * 2. Nothing required. A required config key is something the wizard cannot
 *    answer for the reader: a base URL for a self-hosted service (Subsarr), a
 *    Whisper endpoint, or credentials that are not named like credentials
 *    (BetaSeries calls its API key a token, and does not mark it secret).
 * 3. No helper service. A provider that declares FlareSolverr or an anti-captcha
 *    solver reaches out to something that runs outside Bazarr+.
 *
 * A manifest says a provider CAN use a helper, never whether it MUST, and the
 * catalog documents at least one provider (Prijevodi-Online) as needing one to
 * work at all. So condition 3 reads the capability, which keeps the set on the
 * safe side of what the button claims: everything in it works with nothing
 * running but Bazarr+.
 *
 * The cost of that is over-exclusion. OpenSubtitles.org and the other
 * ai-cloudscraper providers document their helper as a fallback and would work
 * without it, yet they are left out. They stay one tick away in the same list,
 * and the narrower rule needs a necessity flag in the catalog, which this repo
 * cannot add.
 */
export function isRecommendedProvider(
  candidate: RecommendedCandidate | null | undefined,
): boolean {
  if (!candidate || !candidate.manifest) {
    return false;
  }
  const { manifest } = candidate;
  if (detectAuthFromManifest(manifest) !== "none") {
    return false;
  }
  if (requiredConfigKeys(manifest).length > 0) {
    return false;
  }
  return !usesFlaresolverr(candidate) && !usesAntiCaptcha(candidate);
}
