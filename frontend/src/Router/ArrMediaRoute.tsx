import type { PropsWithChildren } from "react";
import { useSystemSettings } from "@/apis/hooks/system";
import FolderMediaNotice from "@/pages/views/FolderMediaNotice";

export default function ArrMediaRoute({
  kind,
  children,
}: PropsWithChildren<{ kind: "sonarr" | "radarr" }>) {
  const { data } = useSystemSettings();
  if (!data) return null;

  // Source navigation can open these addresses while the existing pages still
  // read and mutate the entire Arr library. Mount them only for the Arr route.
  if (data.media_sources?.visible && !data.general[`use_${kind}`]) {
    return <FolderMediaNotice />;
  }
  return children;
}
