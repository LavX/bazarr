import { FunctionComponent } from "react";
import { Group, Pagination, Select, Text } from "@mantine/core";
import { useIsLoading } from "@/contexts";

interface Props {
  count: number;
  index: number;
  size: number;
  total: number;
  goto: (idx: number) => void;
  onPageSizeChange?: (size: number) => void;
}

const PageControl: FunctionComponent<Props> = ({
  count,
  index,
  size,
  total,
  goto,
  onPageSizeChange,
}) => {
  const empty = total === 0;
  const start = empty ? 0 : size * index + 1;
  const end = Math.min(size * (index + 1), total);

  const isLoading = useIsLoading();

  return (
    <Group p={16} justify="space-between">
      <Group gap="md">
        <Text size="sm">
          Show {start} to {end} of {total} entries
        </Text>
        {onPageSizeChange && (
          <Select
            size="xs"
            value={size >= total && total > 0 ? "all" : String(size)}
            onChange={(val) => {
              if (val === "all") {
                onPageSizeChange(total);
              } else {
                onPageSizeChange(Number(val));
              }
              goto(0);
            }}
            data={[
              { value: "25", label: "25 per page" },
              { value: "50", label: "50 per page" },
              { value: "100", label: "100 per page" },
              { value: "250", label: "250 per page" },
              { value: "all", label: "All" },
            ]}
            w={130}
            allowDeselect={false}
          />
        )}
      </Group>
      <Pagination
        size="sm"
        color={isLoading ? "gray" : "primary"}
        value={index + 1}
        onChange={(page) => {
          return goto(page - 1);
        }}
        hidden={count <= 1}
        total={count}
      ></Pagination>
    </Group>
  );
};

export default PageControl;
