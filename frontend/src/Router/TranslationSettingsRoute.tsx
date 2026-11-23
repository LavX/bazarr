import { FunctionComponent } from "react";
import { Navigate, useLocation } from "react-router";
import SettingsTranslatorView from "@/pages/Settings/Translator";
import SystemStatusGate from "./SystemStatusGate";

export const TranslationRedirect: FunctionComponent = () => {
  const { search, hash } = useLocation();
  return <Navigate to={{ pathname: "/translation", search, hash }} replace />;
};

const TranslationSettingsRoute: FunctionComponent = () => {
  return (
    <SystemStatusGate unavailableFallback={<TranslationRedirect />}>
      {(runtime) =>
        runtime === "ready" ? (
          <TranslationRedirect />
        ) : (
          <SettingsTranslatorView />
        )
      }
    </SystemStatusGate>
  );
};

export default TranslationSettingsRoute;
