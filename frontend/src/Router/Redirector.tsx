import { FunctionComponent, useEffect } from "react";
import { useNavigate } from "react-router";
import { LoadingOverlay } from "@mantine/core";
import { useSystemSettings } from "@/apis/hooks";
import { useOnboardingState } from "@/pages/Setup/useOnboardingState";

const Redirector: FunctionComponent = () => {
  const { data } = useSystemSettings();
  const { needsOnboarding, isLoading } = useOnboardingState();

  const navigate = useNavigate();

  useEffect(() => {
    // Both reads have to have answered before this decides. needsOnboarding is
    // true by default while they are in flight, so choosing early sends a
    // configured install to the wizard, and choosing on settings alone sends a
    // fresh one past it. Neither is recoverable: this replaces the entry, so
    // the reader never sees a history entry for what they were sent past.
    if (isLoading || data === undefined) return;

    // A fresh install meets the wizard before any normal routing.
    if (needsOnboarding) {
      navigate("/setup", { replace: true });
      return;
    }

    // Everything else, configured or not, opens on Discover.
    navigate("/discover", { replace: true });
  }, [data, navigate, needsOnboarding, isLoading]);

  return <LoadingOverlay visible></LoadingOverlay>;
};

export default Redirector;
