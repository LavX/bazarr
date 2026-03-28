import { FunctionComponent, useMemo } from "react";
import { Button, Stack, Text } from "@mantine/core";
import { useLanguageProfiles } from "@/apis/hooks";
import { useModals, withModal } from "@/modules/modals";

interface ChangeProfileFormProps {
  onSelect: (profileId: number | null) => void;
}

const ChangeProfileForm: FunctionComponent<ChangeProfileFormProps> = ({
  onSelect,
}) => {
  const { data: profiles } = useLanguageProfiles();
  const modals = useModals();

  const sortedProfiles = useMemo(
    () => (profiles ?? []).slice().sort((a, b) => a.name.localeCompare(b.name)),
    [profiles],
  );

  const handleSelect = (profileId: number | null) => {
    onSelect(profileId);
    modals.closeSelf();
  };

  return (
    <Stack gap="xs">
      <Button
        variant="subtle"
        color="red"
        size="sm"
        onClick={() => handleSelect(null)}
        justify="flex-start"
      >
        Clear Profile
      </Button>
      {sortedProfiles.map((profile) => (
        <Button
          key={profile.profileId}
          variant="subtle"
          size="sm"
          onClick={() => handleSelect(profile.profileId)}
          justify="flex-start"
        >
          <Text size="sm">{profile.name}</Text>
        </Button>
      ))}
    </Stack>
  );
};

export const ChangeProfileModal = withModal(
  ChangeProfileForm,
  "change-profile",
  {
    title: "Change Profile",
    size: "sm",
  },
);

export default ChangeProfileForm;
