import { useCallback } from "react";
import { useSystemSettings } from "@/apis/hooks";
import { writeStoredValue } from "./browserStorage";

export const uiPageSizeKey = "settings-general-page_size";

export function useUpdateLocalStorage() {
  return useCallback((newVals: LooseObject) => {
    for (const key in newVals) {
      writeStoredValue(key, newVals[key]);
    }
  }, []);
}

export function usePageSize() {
  const settings = useSystemSettings();

  return settings.data?.general?.page_size ?? 50;
}
