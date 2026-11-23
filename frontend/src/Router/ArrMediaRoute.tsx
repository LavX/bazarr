import type { PropsWithChildren } from "react";
import {
  isArrMediaRouteEnabled,
  useArrInstances,
} from "@/apis/hooks/arrInstances";
import { useSystemSettings } from "@/apis/hooks/system";
import FolderMediaNotice from "@/pages/views/FolderMediaNotice";

export default function ArrMediaRoute({
  kind,
  children,
}: PropsWithChildren<{ kind: "sonarr" | "radarr" }>) {
  const { data } = useSystemSettings();
  const visible = data?.media_sources?.visible === true;
  const master = data?.general[`use_${kind}`] === true;
  const instances = useArrInstances(visible && master);
  if (!data) return null;
  if (!visible) return children;
  if (master && instances.isLoading) return null;

  return isArrMediaRouteEnabled(kind, data, instances.data) ? (
    children
  ) : (
    <FolderMediaNotice />
  );
}
