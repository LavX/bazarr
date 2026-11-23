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
  sourceBrowsable = false,
}: PropsWithChildren<{
  kind: "sonarr" | "radarr";
  sourceBrowsable?: boolean;
}>) {
  const { data } = useSystemSettings();
  const master = data?.general[`use_${kind}`] === true;
  const instances = useArrInstances(master);
  if (!data) return null;
  if (sourceBrowsable) return children;
  if (master && instances.isLoading) return null;

  return isArrMediaRouteEnabled(kind, data, instances.data) ? (
    children
  ) : (
    <FolderMediaNotice />
  );
}
