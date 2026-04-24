import { FunctionComponent } from "react";
import Markdown from "react-markdown";
import {
  Badge,
  Card,
  Container,
  Divider,
  Group,
  Stack,
  Text,
} from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import { useSystemReleases } from "@/apis/hooks";
import { useInstanceName } from "@/apis/hooks/site";
import { QueryOverlay } from "@/components/async";
import { BuildKey } from "@/utilities";
import classes from "./index.module.css";

const SystemReleasesView: FunctionComponent = () => {
  const releases = useSystemReleases();
  const { data } = releases;

  useDocumentTitle(`Releases - ${useInstanceName()} (System)`);

  return (
    <Container size="md" py={12}>
      <QueryOverlay result={releases}>
        <Stack gap="lg">
          {data?.map((v, idx) => (
            <ReleaseCard key={BuildKey(idx, v.date)} {...v}></ReleaseCard>
          ))}
        </Stack>
      </QueryOverlay>
    </Container>
  );
};

const ReleaseCard: FunctionComponent<ReleaseInfo> = ({
  name,
  body,
  date,
  prerelease,
  current,
  repo,
}) => {
  const normalized = Array.isArray(body) ? body.join("\n\n") : body;

  return (
    <Card p="lg">
      <Group>
        {repo && <Badge color="grape">{repo}</Badge>}
        <Text fw="bold">{name}</Text>
        <Badge color="blue">{date}</Badge>
        <Badge color={prerelease ? "yellow" : "green"}>
          {prerelease ? "Development" : "Master"}
        </Badge>
        {current && <Badge color="indigo">Installed</Badge>}
      </Group>
      <Divider my="sm"></Divider>
      <div className={classes.markdown}>
        <Markdown>{normalized}</Markdown>
      </div>
    </Card>
  );
};

export default SystemReleasesView;
