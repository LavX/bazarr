import { FunctionComponent } from "react";
import { Navigate, useLocation } from "react-router";
import { LoadingOverlay } from "@mantine/core";
import { useSystemStatus } from "@/apis/hooks/system";
import SettingsTranslatorView from "@/pages/Settings/Translator";

export const TranslationRedirect: FunctionComponent = () => {
  const { search, hash } = useLocation();
  return <Navigate to={{ pathname: "/translation", search, hash }} replace />;
};

const TranslationSettingsRoute: FunctionComponent = () => {
  const { data, isPending } = useSystemStatus();
  if (isPending) {
    return <LoadingOverlay visible />;
  }
  if (data?.translation_runtime_v2 === true) {
    return <TranslationRedirect />;
  }
  return <SettingsTranslatorView />;
};

export default TranslationSettingsRoute;
