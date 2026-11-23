import type { PropsWithChildren } from "react";
import { useArrInstances } from "@/apis/hooks/arrInstances";
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
  if (!master) return <FolderMediaNotice />;
  if (instances.isLoading) return null;

  // Once there are instance records, they govern access to the legacy Arr
  // pages. A scalar connection can predate those records on an older install.
  const records = instances.data?.filter((instance) => instance.kind === kind);
  const legacy = data[kind];
  const hasLegacyConnection =
    records?.length === 0 &&
    Boolean(legacy?.ip?.trim() && legacy.apikey?.trim());
  return records?.some((instance) => instance.enabled) ||
    hasLegacyConnection ? (
    children
  ) : (
    <FolderMediaNotice />
  );
}
