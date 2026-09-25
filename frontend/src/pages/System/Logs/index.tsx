import { FunctionComponent, useCallback, useEffect, useState } from "react";
import {
  Alert,
  Badge,
  Code,
  Container,
  Group,
  Select,
  Stack,
  Text,
  TextInput,
  UnstyledButton,
} from "@mantine/core";
import { useDebouncedValue, useDocumentTitle } from "@mantine/hooks";
import { useModals } from "@mantine/modals";
import {
  faDownload,
  faFilter,
  faSearch,
  faSync,
  faTimes,
  faTrash,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useDeleteLogs, useSystemLogs, useSystemSettings } from "@/apis/hooks";
import { useAppTitle } from "@/apis/hooks/site";
import { Toolbox } from "@/components";
import { QueryOverlay } from "@/components/async";
import PageControl from "@/components/tables/PageControl";
import { LoadingProvider } from "@/contexts";
import {
  Check,
  LayoutModal,
  Message,
  Text as SettingText,
} from "@/pages/Settings/components";
import { Environment, ScrollToTop } from "@/utilities";
import { usePageSize } from "@/utilities/storage";
import Table from "./table";
import styles from "./Logs.module.scss";

const ALL_LEVELS = "all";

// A minimum severity, sent to the server as the level parameter.
const LEVEL_OPTIONS: { value: string; label: string }[] = [
  { value: ALL_LEVELS, label: "All levels" },
  { value: "info", label: "Info and above" },
  { value: "warning", label: "Warnings and above" },
  { value: "error", label: "Errors and above" },
  { value: "critical", label: "Critical only" },
];

const SystemLogsView: FunctionComponent = () => {
  const pageSize = usePageSize();
  const [level, setLevel] = useState<System.LogLevel | undefined>();
  const [search, setSearch] = useState("");
  const [contains] = useDebouncedValue(search.trim(), 300);
  const [page, setPage] = useState(0);

  // A changed filter starts again from the newest entry.
  const filterKey = `${level ?? ""}\n${contains}\n${pageSize}`;
  const [pageFilterKey, setPageFilterKey] = useState(filterKey);
  if (pageFilterKey !== filterKey) {
    setPageFilterKey(filterKey);
    setPage(0);
  }

  const logs = useSystemLogs({ page, pageSize, level, contains });
  const { isFetching, isLoading, isPlaceholderData, data, refetch } = logs;
  const total = data?.total ?? 0;
  const pageCount = Math.ceil(total / pageSize);
  const filterErrors = data?.filter_errors ?? [];

  // Emptying the log, or new lines under a narrower filter, can leave the
  // page past the end.
  useEffect(() => {
    if (!isPlaceholderData && pageCount > 0 && page >= pageCount) {
      setPage(pageCount - 1);
    }
  }, [isPlaceholderData, page, pageCount]);

  useEffect(() => {
    ScrollToTop();
  }, [page]);

  const { mutate, isPending } = useDeleteLogs();

  const download = useCallback(() => {
    window.open(`${Environment.baseUrl}/bazarr.log`);
  }, []);

  useDocumentTitle(`Logs - ${useAppTitle()} (System)`);

  const { data: settings } = useSystemSettings();
  const modals = useModals();

  const suffix = () => {
    const include = settings?.log.include_filter;
    const exclude = settings?.log.exclude_filter;
    const includeIndex = include !== "" && include !== undefined ? 1 : 0;
    const excludeIndex = exclude !== "" && exclude !== undefined ? 1 : 0;
    const filters = [
      ["", "I"],
      ["E", "I/E"],
    ];
    const filterStr = filters[excludeIndex][includeIndex];
    const debugStr = settings?.general.debug ? "Debug" : "";
    const spaceStr = debugStr !== "" && filterStr !== "" ? " " : "";
    const suffixStr = debugStr + spaceStr + filterStr;
    return suffixStr;
  };

  const openFilterModal = () => {
    const callbackModal = (close: boolean) => {
      if (close) {
        modals.closeModal(id);
      }
    };

    const id = modals.openModal({
      title: "Set Log Debug and Filter Options",
      children: (
        <LayoutModal callbackModal={callbackModal}>
          <Stack>
            <Check label="Debug" settingKey="settings-general-debug"></Check>
            <Message>Debug logging should only be enabled temporarily</Message>
            <SettingText
              label="Include Filter"
              settingKey="settings-log-include_filter"
            ></SettingText>
            <SettingText
              label="Exclude Filter"
              settingKey="settings-log-exclude_filter"
            ></SettingText>
            <Check
              label="Use Regular Expressions (Regex)"
              settingKey="settings-log-use_regex"
            ></Check>
            <Check
              label="Ignore Case"
              settingKey="settings-log-ignore_case"
            ></Check>
          </Stack>
        </LayoutModal>
      ),
    });
  };

  const filtered = level !== undefined || contains !== "";

  return (
    <Container fluid px={0}>
      <QueryOverlay result={logs}>
        <Toolbox>
          <Group gap="xs">
            <Toolbox.Button
              loading={isFetching}
              icon={faSync}
              onClick={() => refetch()}
            >
              Refresh
            </Toolbox.Button>
            <Toolbox.Button icon={faDownload} onClick={download}>
              Download
            </Toolbox.Button>
            <Toolbox.Button
              loading={isPending}
              icon={faTrash}
              onClick={() => mutate()}
            >
              Empty
            </Toolbox.Button>
            <Toolbox.Button
              loading={isPending}
              icon={faFilter}
              onClick={openFilterModal}
              rightSection={
                suffix() !== "" ? (
                  <Badge size="xs" radius="sm">
                    {suffix()}
                  </Badge>
                ) : (
                  <></>
                )
              }
            >
              Filter
            </Toolbox.Button>
          </Group>
        </Toolbox>
        <Group className={styles.filters} gap="xs">
          <Select
            aria-label="Minimum level"
            className={styles.level}
            data={LEVEL_OPTIONS}
            value={level ?? ALL_LEVELS}
            onChange={(value) =>
              setLevel(
                value && value !== ALL_LEVELS
                  ? (value as System.LogLevel)
                  : undefined,
              )
            }
            allowDeselect={false}
            size="sm"
          />
          <TextInput
            aria-label="Filter log entries"
            className={styles.search}
            placeholder="Filter by text or logger name"
            leftSection={
              <FontAwesomeIcon icon={faSearch} size="sm" opacity={0.5} />
            }
            rightSection={
              search.length > 0 ? (
                <UnstyledButton
                  className={styles.clear}
                  onClick={() => setSearch("")}
                  aria-label="Clear filter"
                >
                  <FontAwesomeIcon icon={faTimes} size="sm" opacity={0.5} />
                </UnstyledButton>
              ) : undefined
            }
            value={search}
            onChange={(event) => setSearch(event.currentTarget.value)}
            size="sm"
          />
        </Group>
        {filterErrors.length > 0 && (
          <Alert
            className={styles.alert}
            color="red"
            variant="light"
            title="A stored filter is not applied"
          >
            <Stack gap={4}>
              {filterErrors.map((error) => (
                <Text size="sm" key={error.filter}>
                  The {error.filter} filter <Code>{error.pattern}</Code> is not
                  a valid regular expression ({error.message}), so these entries
                  are not filtered by it. Correct it under Filter.
                </Text>
              ))}
            </Stack>
          </Alert>
        )}
        <LoadingProvider value={isLoading || (isFetching && isPlaceholderData)}>
          <Table
            logs={data?.data ?? []}
            emptyText={
              filtered
                ? "No log entries match these filters"
                : "The log is empty"
            }
          ></Table>
          <PageControl
            count={pageCount}
            index={page}
            size={pageSize}
            total={total}
            goto={setPage}
          ></PageControl>
        </LoadingProvider>
      </QueryOverlay>
    </Container>
  );
};

export default SystemLogsView;
