import { useIsMutating, useMutation } from "@tanstack/react-query";
import { useSettingsMutation } from "@/apis/hooks";
import api from "@/apis/raw";
import type { MediaServerKind } from "@/apis/raw/mediaServers";

export const MEDIA_SERVER_DISCONNECT_KEY = "setup-media-server-disconnect";

export function useMediaServerDisconnectPending() {
  return useIsMutating({ mutationKey: [MEDIA_SERVER_DISCONNECT_KEY] }) > 0;
}

type RefreshWrite =
  | { switches: LooseObject }
  | { disableIfEmpty: MediaServerKind };

export function useSetupMediaServerRefreshes(inlineErrors = false) {
  const settings = useSettingsMutation(false, { inlineErrors });
  return useMutation({
    // A form can save while a delete is pending. Keep its enable write ordered
    // with the disconnect's list check and any resulting disable write.
    scope: { id: "setup-media-server-refreshes" },
    // HTTP remains available when the websocket reports offline. Let queued
    // work resume under the same HTTP behavior as the settings mutation.
    networkMode: "always",
    mutationFn: async (write: RefreshWrite) => {
      if ("switches" in write) {
        await settings.mutateAsync(write.switches);
      } else {
        const kind = write.disableIfEmpty;
        const remaining = await api.mediaServers.list(kind);
        if (remaining.length === 0) {
          await settings.mutateAsync({
            [`settings-general-use_${kind}`]: false,
          });
        }
      }
    },
  });
}
