import { type ReactNode, useState } from "react";
import {
  Alert,
  Button,
  Group,
  Modal,
  Skeleton,
  Stack,
  Text,
  ThemeIcon,
} from "@mantine/core";
import {
  faFolderOpen,
  faPlus,
  faTriangleExclamation,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useLanguageProfiles } from "@/apis/hooks/languages";
import {
  mediaSourceErrorMessage,
  useMediaSources,
  useRemoveMediaSource,
} from "@/apis/hooks/mediaSources";
import type { MediaSource, SourceLibrary } from "@/apis/raw/mediaSources";
import { Section } from "@/pages/Settings/components";
import SourceCard from "./SourceCard";
import SourceFormModal, { type SourceEditor } from "./SourceFormModal";
import styles from "./Connections.module.scss";

type Removal = { source: MediaSource; library?: SourceLibrary };

function RemoveSourceModal({
  target: { source, library },
  onClose,
}: {
  target: Removal;
  onClose: () => void;
}) {
  const remove = useRemoveMediaSource();
  const action = library ? "Remove library" : "Remove source";
  const confirm = () => {
    remove.mutate({ source, library }, { onSuccess: onClose });
  };

  return (
    <Modal
      opened
      onClose={remove.isPending ? () => undefined : onClose}
      title={action}
      closeOnClickOutside={!remove.isPending}
      closeOnEscape={!remove.isPending}
      withCloseButton={!remove.isPending}
    >
      <Stack gap="md">
        <Text size="sm" style={{ overflowWrap: "anywhere" }}>
          {library
            ? `Remove ${library.locator} and its media items from Bazarr+?`
            : `Remove ${source.name}, its libraries and their media items from Bazarr+?`}
        </Text>
        <Text size="sm">
          Bazarr+ keeps the subtitle history, blacklist entries, and files on
          disk.
        </Text>
        {remove.isError && (
          <Alert role="alert" color="red" title="Could not remove">
            {mediaSourceErrorMessage(remove.error, "Try again.")}
          </Alert>
        )}
        <Group justify="flex-end">
          <Button
            variant="default"
            onClick={onClose}
            disabled={remove.isPending}
            data-autofocus
          >
            Cancel
          </Button>
          <Button color="red" onClick={confirm} loading={remove.isPending}>
            {action}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

export default function SourceSection() {
  const query = useMediaSources();
  const { data: profiles } = useLanguageProfiles();
  const [editor, setEditor] = useState<SourceEditor | null>(null);
  const [removal, setRemoval] = useState<Removal | null>(null);
  const folderSources =
    query.data?.data.filter(({ kind }) => kind === "folder") ?? [];
  const profileNames = new Map(
    profiles?.map(({ profileId, name }) => [profileId, name]),
  );
  const openCreate = () => setEditor({ mode: "create-source" });

  let content: ReactNode;
  if (query.isLoading) {
    content = <Skeleton height={96} radius="lg" />;
  } else if (query.isError) {
    content = (
      <Alert
        role="alert"
        color="red"
        icon={<FontAwesomeIcon icon={faTriangleExclamation} />}
        title="Could not load folder sources"
      >
        <Group justify="space-between">
          <Text size="sm">Check that Bazarr+ is reachable and try again.</Text>
          <Button
            size="xs"
            variant="light"
            color="red"
            onClick={() => void query.refetch()}
          >
            Retry
          </Button>
        </Group>
      </Alert>
    );
  } else if (folderSources.length === 0) {
    content = (
      <div className={styles.empty}>
        <ThemeIcon variant="light" color="brand" size={46} radius="xl">
          <FontAwesomeIcon icon={faFolderOpen} />
        </ThemeIcon>
        <Text fw={600}>No folder sources yet</Text>
        <Text size="sm" c="dimmed" maw={440}>
          Add a folder that Bazarr+ can read to manage subtitles for the media
          inside it.
        </Text>
        <Button
          mt="xs"
          variant="light"
          leftSection={<FontAwesomeIcon icon={faPlus} />}
          onClick={openCreate}
        >
          Add folder source
        </Button>
      </div>
    );
  } else {
    content = (
      <Stack gap="sm">
        <Group justify="flex-end">
          <Button
            type="button"
            size="xs"
            variant="light"
            leftSection={<FontAwesomeIcon icon={faPlus} />}
            onClick={openCreate}
          >
            Add folder source
          </Button>
        </Group>
        {folderSources.map((source) => (
          <SourceCard
            key={source.id}
            source={source}
            profileNames={profileNames}
            onEdit={setEditor}
            onRemove={(target, library) =>
              setRemoval({ source: target, library })
            }
          />
        ))}
      </Stack>
    );
  }

  return (
    <Section header="Local folders">
      {content}
      {editor && (
        <SourceFormModal editor={editor} onClose={() => setEditor(null)} />
      )}
      {removal && (
        <RemoveSourceModal target={removal} onClose={() => setRemoval(null)} />
      )}
    </Section>
  );
}
