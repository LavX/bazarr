import { useEffect } from "react";
import { Link } from "react-router";
import { Alert, Anchor, Stack, Text } from "@mantine/core";
import { useQueryClient } from "@tanstack/react-query";
import { useSportsJob } from "@/apis/hooks/sports";
import { QueryKeys } from "@/apis/queries/keys";
import type { SportsJob } from "@/apis/raw/sports";

export default function SportsJobFeedback({
  queued,
  owner,
}: {
  queued?: SportsJob;
  owner?: number;
}) {
  const job = useSportsJob(queued?.job_id, owner);
  const client = useQueryClient();
  const status = job.data;
  useEffect(() => {
    if (status?.status === "completed" || status?.status === "failed") {
      void client.invalidateQueries({ queryKey: [QueryKeys.Sports] });
      void client.invalidateQueries({ queryKey: [QueryKeys.Badges] });
    }
  }, [client, queued?.job_id, status?.status]);
  if (!queued) return null;
  const result = status?.result;
  return (
    <Alert color={status?.status === "failed" ? "red" : "blue"}>
      <Stack gap={4}>
        <Text size="sm">
          {status?.cancelled
            ? "Search cancelled"
            : status?.message || queued.message}
        </Text>
        {result?.file_status && (
          <Text size="sm">Subtitle file: {result.file_status}.</Text>
        )}
        {result?.replacement?.message && (
          <Text size="sm">{result.replacement.message}</Text>
        )}
        {result?.data?.map((outcome, index) =>
          outcome.message && outcome.message !== status?.message ? (
            <Text size="sm" key={index}>
              {outcome.message}
            </Text>
          ) : null,
        )}
        {job.isError && (
          <Text size="sm">
            This job is no longer available for the selected instance.
          </Text>
        )}
        <Anchor component={Link} to="/system/tasks">
          View jobs or stop a running search
        </Anchor>
      </Stack>
    </Alert>
  );
}
