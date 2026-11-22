import { Link } from "react-router";
import { Anchor, Container, Text } from "@mantine/core";

export default function FolderMediaNotice() {
  return (
    <Container px={0} fluid>
      <Text p="md">
        Manage local folders in{" "}
        <Anchor component={Link} to="/settings/connections#folders">
          Connections
        </Anchor>
        .
      </Text>
    </Container>
  );
}
