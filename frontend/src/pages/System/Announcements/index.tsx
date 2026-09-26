import { FunctionComponent } from "react";
import { Card, Container, Skeleton, Stack, Text } from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import { useSystemAnnouncements } from "@/apis/hooks";
import { useAppTitle } from "@/apis/hooks/site";
import { QueryOverlay } from "@/components/async";
import { useIsLoading } from "@/contexts";
import { BuildKey } from "@/utilities";
import AnnouncementCard from "./Card";

const PLACEHOLDERS = 3;

/**
 * The list, not the page, so it can read the loading flag QueryOverlay
 * publishes. Without it the initial request would render as an empty state:
 * the announcements have not arrived yet, which is not the same as there being
 * none.
 */
const AnnouncementList: FunctionComponent<{
  announcements: System.Announcements[];
}> = ({ announcements }) => {
  const isLoading = useIsLoading();

  if (isLoading) {
    return (
      <Stack gap="lg">
        {Array(PLACEHOLDERS)
          .fill(0)
          .map((_, i) => (
            <Card key={i} p="lg">
              <Skeleton height={16} width={112} radius="sm"></Skeleton>
              <Skeleton height={22} mt="sm" width="55%" radius="sm"></Skeleton>
              <Skeleton height={14} mt="sm"></Skeleton>
              <Skeleton height={14} mt={6}></Skeleton>
              <Skeleton height={14} mt={6} width="72%"></Skeleton>
            </Card>
          ))}
      </Stack>
    );
  }

  if (!announcements.length) {
    return (
      <Text c="var(--bz-text-tertiary)" ta="center" py="xl">
        No announcements for now, come back later!
      </Text>
    );
  }

  return (
    <Stack gap="lg">
      {announcements.map((v) => (
        <AnnouncementCard
          key={BuildKey(v.hash, v.text)}
          announcement={v}
        ></AnnouncementCard>
      ))}
    </Stack>
  );
};

const SystemAnnouncementsView: FunctionComponent = () => {
  const announcements = useSystemAnnouncements();

  const { data } = announcements;

  useDocumentTitle(`Announcements - ${useAppTitle()} (System)`);

  return (
    // The measure the release notes use. Announcements are prose, and prose
    // stretched to the width of the window is what made this page unreadable.
    <Container size="md" py={12}>
      <QueryOverlay result={announcements}>
        <AnnouncementList announcements={data ?? []}></AnnouncementList>
      </QueryOverlay>
    </Container>
  );
};

export default SystemAnnouncementsView;
