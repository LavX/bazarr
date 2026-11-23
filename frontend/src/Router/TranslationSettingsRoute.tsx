import { FunctionComponent } from "react";
import { Navigate, useLocation } from "react-router";
import SettingsTranslatorView from "@/pages/Settings/Translator";
import SystemStatusGate from "./SystemStatusGate";

export const TranslationRedirect: FunctionComponent = () => {
  const { search, hash } = useLocation();
  return (
    <SystemStatusGate>
      {() => (
        <Navigate to={{ pathname: "/translation", search, hash }} replace />
      )}
    </SystemStatusGate>
  );
};

const TranslationSettingsRoute: FunctionComponent = () => {
  return (
    <SystemStatusGate>
      {(status) =>
        status.translation_runtime_v2 === true ? (
          <TranslationRedirect />
        ) : (
          <SettingsTranslatorView />
        )
      }
    </SystemStatusGate>
  );
};

export default TranslationSettingsRoute;
