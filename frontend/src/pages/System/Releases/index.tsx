import React, { FunctionComponent, useMemo } from "react";
import {
  Badge,
  Card,
  Code,
  Container,
  Divider,
  Group,
  List,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import { useSystemReleases } from "@/apis/hooks";
import { useInstanceName } from "@/apis/hooks/site";
import { QueryOverlay } from "@/components/async";
import { BuildKey } from "@/utilities";

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

function renderInlineMarkdown(text: string) {
  const parts: (string | React.JSX.Element)[] = [];
  const regex = /`([^`]+)`|\*\*([^*]+)\*\*|\[([^\]]+)\]\([^)]+\)/g;
  let lastIndex = 0;
  let match;

  while ((match = regex.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push(text.slice(lastIndex, match.index));
    }
    if (match[1]) {
      parts.push(<Code key={match.index}>{match[1]}</Code>);
    } else if (match[2]) {
      parts.push(<Text key={match.index} span fw="bold">{match[2]}</Text>);
    } else if (match[3]) {
      parts.push(match[3]);
    }
    lastIndex = match.index + match[0].length;
  }

  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex));
  }

  return parts;
}

function MarkdownBody({ text }: { text: string | string[] }) {
  const elements = useMemo(() => {
    const normalized = Array.isArray(text) ? text.join("\n") : text;
    const lines = normalized.split("\n");
    const result: React.JSX.Element[] = [];
    let listItems: React.JSX.Element[] = [];
    let listKey = 0;

    const flushList = () => {
      if (listItems.length > 0) {
        result.push(<List key={`list-${listKey++}`} size="sm" spacing={4}>{listItems}</List>);
        listItems = [];
      }
    };

    lines.forEach((line, idx) => {
      const trimmed = line.trim();

      if (!trimmed) {
        return;
      }

      if (trimmed.startsWith("### ")) {
        flushList();
        result.push(<Title key={idx} order={5} mt="sm">{trimmed.slice(4)}</Title>);
      } else if (trimmed.startsWith("## ")) {
        flushList();
        result.push(<Title key={idx} order={4} mt="sm">{trimmed.slice(3)}</Title>);
      } else if (/^[-*] /.test(trimmed)) {
        const content = trimmed.replace(/^[-*] /, "");
        listItems.push(<List.Item key={idx}>{renderInlineMarkdown(content)}</List.Item>);
      } else {
        flushList();
        result.push(<Text key={idx} size="sm">{renderInlineMarkdown(trimmed)}</Text>);
      }
    });

    flushList();
    return result;
  }, [text]);

  return <>{elements}</>;
}

const ReleaseCard: FunctionComponent<ReleaseInfo> = ({
  name,
  body,
  date,
  prerelease,
  current,
  repo,
}) => {
  return (
    <Card shadow="md" p="lg">
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
      <MarkdownBody text={body} />
    </Card>
  );
};

export default SystemReleasesView;
