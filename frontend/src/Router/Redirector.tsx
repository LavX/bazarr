import { FunctionComponent, useEffect } from "react";
import { useNavigate } from "react-router";
import { LoadingOverlay } from "@mantine/core";
import { useSystemSettings } from "@/apis/hooks";
import { useOnboardingState } from "@/pages/Setup/useOnboardingState";

const Redirector: FunctionComponent = () => {
  const { data } = useSystemSettings();
  const { needsOnboarding } = useOnboardingState();

  const navigate = useNavigate();

  useEffect(() => {
    // Fresh installs go to the first-run wizard before any normal routing.
    if (needsOnboarding) {
      navigate("/setup", { replace: true });
      return;
    }

    if (data) {
      const { use_sonarr: useSonarr, use_radarr: useRadarr } = data.general;
      if (useSonarr) {
        navigate("/series", { replace: true });
      } else if (useRadarr) {
        navigate("/movies", { replace: true });
      } else {
        navigate("/settings/general", { replace: true });
      }
    }
  }, [data, navigate, needsOnboarding]);

  return <LoadingOverlay visible></LoadingOverlay>;
};

export default Redirector;
