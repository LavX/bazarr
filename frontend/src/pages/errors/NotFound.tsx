import { FunctionComponent } from "react";
import { useNavigate } from "react-router";
import {
  Box,
  Button,
  Center,
  Container,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { faEyeSlash as fasEyeSlash } from "@fortawesome/free-regular-svg-icons";
import { faArrowLeft } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";

const NotFound: FunctionComponent = () => {
  const navigate = useNavigate();

  return (
    <Container my="lg">
      <Center>
        <Stack align="center" gap="md">
          <Title order={1}>
            <Box component="span" mr="md">
              <FontAwesomeIcon icon={fasEyeSlash}></FontAwesomeIcon>
            </Box>
            404
          </Title>
          <Text c="var(--bz-text-secondary)">
            Page not found. The page you requested does not exist.
          </Text>
          <Button
            variant="light"
            leftSection={<FontAwesomeIcon icon={faArrowLeft} />}
            onClick={() => navigate("/")}
          >
            Go to Home
          </Button>
        </Stack>
      </Center>
    </Container>
  );
};

export default NotFound;
