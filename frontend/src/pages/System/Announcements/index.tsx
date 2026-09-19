import { FunctionComponent } from "react";
import { Container, Stack, Text } from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import { useSystemAnnouncements } from "@/apis/hooks";
import { useAppTitle } from "@/apis/hooks/site";
import { QueryOverlay } from "@/components/async";
import { BuildKey } from "@/utilities";
import AnnouncementCard from "./Card";

const SystemAnnouncementsView: FunctionComponent = () => {
  const announcements = useSystemAnnouncements();

  const { data } = announcements;

  useDocumentTitle(`Announcements - ${useAppTitle()} (System)`);

  return (
    // The measure the release notes use. Announcements are prose, and prose
    // stretched to the width of the window is what made this page unreadable.
    <Container size="md" py={12}>
      <QueryOverlay result={announcements}>
        {data?.length ? (
          <Stack gap="lg">
            {data.map((v) => (
              <AnnouncementCard
                key={BuildKey(v.hash, v.text)}
                announcement={v}
              ></AnnouncementCard>
            ))}
          </Stack>
        ) : (
          <Text c="var(--bz-text-tertiary)" ta="center" py="xl">
            No announcements for now, come back later!
          </Text>
        )}
      </QueryOverlay>
    </Container>
  );
};

export default SystemAnnouncementsView;
