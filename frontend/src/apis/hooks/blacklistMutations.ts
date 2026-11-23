import { showNotification } from "@mantine/notifications";
import { QueryKey, useMutation, useQueryClient } from "@tanstack/react-query";
import { isAxiosError } from "axios";
import { notification } from "@/modules/notification";

export function useDeleteBlacklistMutation(
  queryKey: QueryKey,
  remove: (removal: Blacklist.Removal) => Promise<void>,
) {
  const client = useQueryClient();
  const refresh = () => client.invalidateQueries({ queryKey });
  return useMutation({
    mutationKey: queryKey,
    mutationFn: remove,
    onSuccess: refresh,
    onError: async (error) => {
      if (!isAxiosError(error) || error.response?.status !== 409) {
        return;
      }
      await refresh();
      showNotification(
        notification.info(
          "Exclusion changed",
          "Check the list before trying again.",
        ),
      );
    },
  });
}
