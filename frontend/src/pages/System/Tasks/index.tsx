import { FunctionComponent } from "react";
import { Link } from "react-router";
import { Anchor, Container, Text } from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import { faSync } from "@fortawesome/free-solid-svg-icons";
import { useSystemTasks } from "@/apis/hooks";
import { useAppTitle } from "@/apis/hooks/site";
import { Toolbox } from "@/components";
import { QueryOverlay } from "@/components/async";
import Table from "./table";

const SystemTasksView: FunctionComponent = () => {
  const tasks = useSystemTasks();

  const { isFetching, data, refetch } = tasks;

  useDocumentTitle(`Tasks - ${useAppTitle()} (System)`);

  return (
    <QueryOverlay result={tasks}>
      <Container fluid px={0}>
        <Toolbox>
          <Toolbox.Button
            loading={isFetching}
            icon={faSync}
            onClick={() => refetch()}
          >
            Refresh
          </Toolbox.Button>
        </Toolbox>
        <Text size="sm" c="dimmed" mb="sm">
          To change how often a task runs, use the{" "}
          <Anchor component={Link} to="/settings/scheduler">
            Scheduler settings
          </Anchor>
          .
        </Text>
        <Table tasks={data ?? []}></Table>
      </Container>
    </QueryOverlay>
  );
};

export default SystemTasksView;
