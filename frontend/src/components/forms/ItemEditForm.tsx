import { FunctionComponent, useMemo } from "react";
import {
  Button,
  Center,
  Divider,
  Group,
  Loader,
  LoadingOverlay,
  Stack,
} from "@mantine/core";
import { useForm } from "@mantine/form";
import { showNotification } from "@mantine/notifications";
import { UseMutationResult } from "@tanstack/react-query";
import { useLanguageProfiles } from "@/apis/hooks";
import { MultiSelector, Selector } from "@/components/inputs";
import { useModals, withModal } from "@/modules/modals";
import { notification } from "@/modules/task";
import { GetItemId, useSelectorOptions } from "@/utilities";

interface Props {
  mutation: UseMutationResult<void, unknown, FormType.ModifyItem, unknown>;
  item: Item.Base | null;
  onComplete?: () => void;
  onCancel?: () => void;
}

interface FormBodyProps extends Props {
  profiles: Language.Profile[];
  isFetching: boolean;
}

const ItemEditFormBody: FunctionComponent<FormBodyProps> = ({
  mutation,
  item,
  onComplete,
  onCancel,
  profiles,
  isFetching,
}) => {
  const { isPending, mutate } = mutation;
  const modals = useModals();

  const profileOptions = useSelectorOptions(
    profiles,
    (v) => v.name ?? "Unknown",
    (v) => v.profileId.toString() ?? "-1",
  );

  // profiles is guaranteed loaded here, so the resolved profile is captured
  // correctly when the form mounts (see the load gate in ItemEditForm).
  const profile = useMemo(
    () => profiles.find((v) => v.profileId === item?.profileId) ?? null,
    [profiles, item?.profileId],
  );

  const form = useForm({
    initialValues: {
      profile: profile ?? null,
    },
  });

  // Item code2 may be undefined or null if the audio language is Unknown
  const options = useSelectorOptions(
    item?.audio_language ?? [],
    (v) => v.name,
    (v) => v.code2 ?? "",
  );

  const isOverlayVisible = isPending || isFetching || item === null;

  return (
    <form
      onSubmit={form.onSubmit(({ profile }) => {
        if (item) {
          const itemId = GetItemId(item);
          if (itemId) {
            mutate(
              { id: [itemId], profileid: [profile?.profileId ?? null] },
              {
                onSuccess: () => {
                  showNotification(
                    notification.info(
                      "Profile Saved",
                      "Languages profile updated successfully",
                    ),
                  );
                  onComplete?.();
                  modals.closeSelf();
                },
                onError: () => {
                  showNotification(
                    notification.error(
                      "Save Failed",
                      "Could not update languages profile",
                    ),
                  );
                },
              },
            );
            return;
          }
        }

        form.setErrors({ profile: "Invalid profile" });
      })}
    >
      <LoadingOverlay visible={isOverlayVisible}></LoadingOverlay>
      <Stack>
        <MultiSelector
          label="Audio Languages"
          disabled
          {...options}
          value={item?.audio_language ?? []}
        ></MultiSelector>
        <Selector
          {...profileOptions}
          {...form.getInputProps("profile")}
          clearable
          label="Languages Profile"
        ></Selector>
        <Divider></Divider>
        <Group justify="right">
          <Button
            disabled={isOverlayVisible}
            onClick={() => {
              onCancel?.();
              modals.closeSelf();
            }}
            color="gray"
            variant="subtle"
          >
            Cancel
          </Button>
          <Button disabled={isOverlayVisible} type="submit">
            Save
          </Button>
        </Group>
      </Stack>
    </form>
  );
};

const ItemEditForm: FunctionComponent<Props> = (props) => {
  const { data, isFetching } = useLanguageProfiles();

  // Gate the form on loaded profiles. If profiles are not cached yet, mounting
  // the form would capture a null initial profile that is never synced once
  // data arrives, so saving would clear the item profile (profileid: [null]).
  if (data === undefined) {
    return (
      <Center my="xl">
        <Loader />
      </Center>
    );
  }

  return (
    <ItemEditFormBody {...props} profiles={data} isFetching={isFetching} />
  );
};

export const ItemEditModal = withModal(ItemEditForm, "item-editor", {
  title: "Editor",
  size: "md",
});

export default ItemEditForm;
