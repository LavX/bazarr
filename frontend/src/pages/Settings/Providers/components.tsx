import {
  CSSProperties,
  Fragment,
  FunctionComponent,
  JSX,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Badge,
  Button,
  Drawer,
  Group,
  MultiSelect,
  Stack,
  Text as MantineText,
  TextInput,
  UnstyledButton,
} from "@mantine/core";
import { useForm } from "@mantine/form";
import {
  closestCenter,
  DndContext,
  DragEndEvent,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import {
  faArrowLeft,
  faCircleInfo,
  faGripVertical,
  faLanguage,
  faMagnifyingGlass,
  faPlus,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { capitalize } from "lodash";
import {
  Check,
  Chips,
  Password,
  ProviderTestButton,
  Selector as GlobalSelector,
  Text,
} from "@/pages/Settings/components";
import {
  FormContext,
  FormValues,
  runHooks,
  useFormActions,
  useStagedValues,
} from "@/pages/Settings/utilities/FormValues";
import { useSettingValue } from "@/pages/Settings/utilities/hooks";
import {
  SettingsProvider,
  useSettings,
} from "@/pages/Settings/utilities/SettingsProvider";
import { BuildKey } from "@/utilities";
import { ASSERT } from "@/utilities/console";
import { ProviderInfo, ProviderList } from "./list";
import {
  ALL_LANGUAGE_OPTIONS,
  AUTH_FILTERS,
  AUTH_LABEL,
  AuthKind,
  getLanguageName,
  getShippedMeta,
} from "./meta";
import styles from "@/pages/Settings/Providers/hub/hub.module.scss";

type SettingsKey =
  | "settings-general-enabled_providers"
  | "settings-general-enabled_integrations";

interface ProviderViewProps {
  addLabel?: string;
  availableOptions: Readonly<ProviderInfo[]>;
  settingsKey: SettingsKey;
}

const parseProviderPriorities = (
  value: unknown,
): Record<string, number> | null => {
  if (!value) {
    return null;
  }

  if (typeof value === "string") {
    try {
      const parsed = JSON.parse(value);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        return parsed as Record<string, number>;
      }
    } catch {
      return null;
    }
  }

  if (typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, number>;
  }

  return null;
};

const resolveProviderPriorities = (
  staged: LooseObject | undefined,
  settings: Settings | null,
): Record<string, number> => {
  const fromStaged = parseProviderPriorities(
    staged?.["settings-general-provider_priorities"],
  );
  if (fromStaged) {
    return { ...fromStaged };
  }

  const fromSettings = parseProviderPriorities(
    settings?.general?.provider_priorities,
  );
  if (fromSettings) {
    return { ...fromSettings };
  }

  return {};
};

interface SortableProviderRowProps {
  provider: ProviderInfo;
  priority: number;
  onSelect: () => void;
}

const SortableProviderRow: FunctionComponent<SortableProviderRowProps> = ({
  provider,
  priority,
  onSelect,
}) => {
  const {
    attributes,
    listeners,
    setNodeRef,
    setActivatorNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: provider.key });

  const clamped = Math.max(1, Math.min(priority, 100));
  const railW = Math.max(2, 6 - (clamped / 100) * 4);
  const displayName = provider.name ?? capitalize(provider.key);
  const meta = getShippedMeta(provider.key, provider.inputs);
  const showAllLangs = meta.languages.length <= LANGUAGE_CHIP_THRESHOLD;
  const langTooltip = meta.languages
    .map((code) => getLanguageName(code))
    .join(", ");
  const rowStyle = {
    transform: CSS.Transform.toString(transform),
    transition,
    "--bz-priority-rail": `${railW.toFixed(2)}px`,
    zIndex: isDragging ? 10 : undefined,
    opacity: isDragging ? 0.85 : undefined,
    boxShadow: isDragging ? "var(--bz-shadow-float)" : undefined,
  } as CSSProperties;

  const handleRowClick = (event: React.MouseEvent) => {
    if (
      (event.target as HTMLElement).closest(`.${styles.priorityDragHandle}`)
    ) {
      return;
    }
    onSelect();
  };

  const handleKeyDown = (event: React.KeyboardEvent) => {
    if (event.target !== event.currentTarget) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onSelect();
    }
  };

  return (
    <div
      ref={setNodeRef}
      className={styles.priorityRow}
      style={rowStyle}
      role="button"
      tabIndex={0}
      aria-label={`${displayName} (priority ${priority})`}
      onClick={handleRowClick}
      onKeyDown={handleKeyDown}
    >
      <span className={styles.priorityRail} aria-hidden="true" />
      <button
        ref={setActivatorNodeRef}
        type="button"
        className={styles.priorityDragHandle}
        aria-label={`Drag to reorder ${displayName}`}
        {...attributes}
        {...listeners}
      >
        <FontAwesomeIcon icon={faGripVertical} />
      </button>
      <span className={styles.priorityBody}>
        <span className={styles.priorityNameRow}>
          <span className={styles.priorityName}>{displayName}</span>
          {provider.source === "plugin" && (
            <Badge size="xs" variant="light">
              Plugin
            </Badge>
          )}
        </span>
        {provider.description && (
          <span
            className={styles.priorityDescription}
            title={provider.description}
          >
            {provider.description}
          </span>
        )}
        <span className={styles.priorityChips}>
          <Badge size="xs" variant="outline" color="gray">
            {AUTH_LABEL[meta.auth]}
          </Badge>
          {meta.testable && (
            <Badge size="xs" variant="outline" color="gray">
              Testable
            </Badge>
          )}
          {meta.languages.length > 0 &&
            showAllLangs &&
            meta.languages.map((code) => (
              <Badge key={code} size="xs" variant="outline" color="gray">
                {getLanguageName(code)}
              </Badge>
            ))}
          {meta.languages.length > LANGUAGE_CHIP_THRESHOLD && (
            <Badge size="xs" variant="outline" color="gray" title={langTooltip}>
              {meta.languages.length} languages
            </Badge>
          )}
        </span>
      </span>
    </div>
  );
};

export const ProviderView: FunctionComponent<ProviderViewProps> = ({
  addLabel,
  availableOptions,
  settingsKey,
}) => {
  const settings = useSettings();
  const staged = useStagedValues();
  const providers = useSettingValue<string[]>(settingsKey);
  const priorities = useMemo(
    () => resolveProviderPriorities(staged, settings),
    [staged, settings],
  );

  const { update } = useFormActions();

  const [drawerPayload, setDrawerPayload] = useState<{
    payload: ProviderInfo | null;
  } | null>(null);

  const select = useCallback(
    (v?: ProviderInfo) => {
      if (settings) {
        setDrawerPayload({ payload: v ?? null });
      }
    },
    [settings],
  );

  const closeDrawer = useCallback(() => setDrawerPayload(null), []);

  const sortedItems = useMemo(() => {
    if (!providers) return [];

    const decorated = providers
      .flatMap((key) => {
        const item = availableOptions.find((opt) => opt.key === key);
        return item ? [item] : [];
      })
      .map((v) => ({ v, priority: priorities[v.key] ?? 100 }));

    decorated.sort((a, b) => {
      if (a.priority !== b.priority) return a.priority - b.priority;
      const an = a.v.name ?? capitalize(a.v.key);
      const bn = b.v.name ?? capitalize(b.v.key);
      return an.localeCompare(bn);
    });

    return decorated;
  }, [providers, availableOptions, priorities]);

  const itemIds = useMemo(
    () => sortedItems.map((item) => item.v.key),
    [sortedItems],
  );

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    }),
  );

  const handleDragEnd = useCallback(
    (event: DragEndEvent) => {
      const { active, over } = event;
      if (!over || active.id === over.id) return;

      const oldIndex = itemIds.indexOf(active.id as string);
      const newIndex = itemIds.indexOf(over.id as string);
      if (oldIndex === -1 || newIndex === -1) return;

      const newOrder = arrayMove(itemIds, oldIndex, newIndex);
      const newPriorities = { ...priorities };
      newOrder.forEach((key, idx) => {
        newPriorities[key] = (idx + 1) * 10;
      });

      update({
        "settings-general-provider_priorities": JSON.stringify(newPriorities),
      });
    },
    [itemIds, priorities, update],
  );

  return (
    <>
      <Stack gap="xs" className={styles.priorityList}>
        <UnstyledButton
          className={styles.priorityAddRow}
          onClick={() => select()}
          aria-label={addLabel ?? "Add provider"}
        >
          <span className={styles.priorityAddIcon} aria-hidden="true">
            <FontAwesomeIcon icon={faPlus} />
          </span>
          <span className={styles.priorityAddText}>
            {addLabel ?? "Add provider"}
          </span>
        </UnstyledButton>
        <DndContext
          sensors={sensors}
          collisionDetection={closestCenter}
          onDragEnd={handleDragEnd}
        >
          <SortableContext
            items={itemIds}
            strategy={verticalListSortingStrategy}
          >
            {sortedItems.map(({ v, priority }) => (
              <SortableProviderRow
                key={v.key}
                provider={v}
                priority={priority}
                onSelect={() => select(v)}
              />
            ))}
          </SortableContext>
        </DndContext>
      </Stack>
      <Drawer
        opened={drawerPayload !== null && settings !== null}
        onClose={closeDrawer}
        position="right"
        size="min(720px, 92vw)"
        padding="md"
        title={
          <MantineText size="lg" fw={600}>
            Provider settings
          </MantineText>
        }
        classNames={{
          content: styles.providerDrawerContent,
          body: styles.providerDrawerBody,
        }}
      >
        {drawerPayload && settings && (
          <ProviderTool
            payload={drawerPayload.payload}
            enabledProviders={providers ?? []}
            staged={staged}
            settings={settings}
            onChange={update}
            availableOptions={availableOptions}
            settingsKey={settingsKey}
            onClose={closeDrawer}
          />
        )}
      </Drawer>
    </>
  );
};

interface ProviderToolProps {
  payload: ProviderInfo | null;
  enabledProviders: readonly string[];
  staged: LooseObject;
  settings: Settings;
  onChange: (v: LooseObject) => void;
  availableOptions: Readonly<ProviderInfo[]>;
  settingsKey: Readonly<SettingsKey>;
  onClose: () => void;
}

const LANGUAGE_CHIP_THRESHOLD = 3;

interface ProviderPickerProps {
  options: ProviderInfo[];
  onPick: (provider: ProviderInfo) => void;
}

const ProviderPicker: FunctionComponent<ProviderPickerProps> = ({
  options,
  onPick,
}) => {
  const [search, setSearch] = useState("");
  const [authFilter, setAuthFilter] = useState<AuthKind | "all">("all");
  const [langFilter, setLangFilter] = useState<string[]>([]);

  const decorated = useMemo(
    () =>
      options.map((v) => ({
        v,
        ...getShippedMeta(v.key, v.inputs),
      })),
    [options],
  );

  const counts = useMemo(() => {
    const acc: Record<string, number> = { all: decorated.length };
    for (const item of decorated) {
      acc[item.auth] = (acc[item.auth] ?? 0) + 1;
    }
    return acc;
  }, [decorated]);

  const availableLanguageCodes = useMemo(() => {
    const set = new Set<string>();
    for (const item of decorated) {
      for (const code of item.languages) set.add(code);
    }
    return set;
  }, [decorated]);

  const languageSelectData = useMemo(
    () =>
      ALL_LANGUAGE_OPTIONS.filter((o) =>
        availableLanguageCodes.has(o.code),
      ).map((o) => ({ value: o.code, label: o.name })),
    [availableLanguageCodes],
  );

  const filtered = useMemo(() => {
    let result = decorated;
    if (authFilter !== "all") {
      result = result.filter((item) => item.auth === authFilter);
    }
    if (langFilter.length > 0) {
      result = result.filter((item) =>
        langFilter.some((code) => item.languages.includes(code)),
      );
    }
    const q = search.trim().toLowerCase();
    if (q) {
      result = result.filter(({ v }) => {
        const name = (v.name ?? v.key).toLowerCase();
        const desc = (v.description ?? "").toLowerCase();
        return name.includes(q) || desc.includes(q);
      });
    }
    return result;
  }, [decorated, authFilter, langFilter, search]);

  return (
    <Stack gap="xs" className={styles.providerPickerStack}>
      <div className={styles.providerPickerFilters}>
        {AUTH_FILTERS.map(({ value, label }) => {
          const count = counts[value] ?? 0;
          const isActive = authFilter === value;
          const isDisabled = value !== "all" && count === 0;
          return (
            <button
              key={value}
              type="button"
              className={
                isActive
                  ? `${styles.providerPickerFilter} ${styles.providerPickerFilterActive}`
                  : styles.providerPickerFilter
              }
              disabled={isDisabled}
              onClick={() => setAuthFilter(value)}
            >
              {label}
              <span className={styles.providerPickerFilterCount}>{count}</span>
            </button>
          );
        })}
      </div>
      <MultiSelect
        data={languageSelectData}
        value={langFilter}
        onChange={setLangFilter}
        placeholder={langFilter.length === 0 ? "Filter by language" : undefined}
        searchable
        clearable
        nothingFoundMessage="No matching language"
        maxDropdownHeight={240}
        comboboxProps={{ withinPortal: true }}
        leftSection={<FontAwesomeIcon icon={faLanguage} />}
      />
      <TextInput
        data-autofocus
        placeholder="Search providers"
        value={search}
        onChange={(e) => setSearch(e.currentTarget.value)}
        leftSection={<FontAwesomeIcon icon={faMagnifyingGlass} />}
      />
      <div className={styles.providerPickerList}>
        {filtered.length === 0 ? (
          <MantineText size="xs" c="dimmed" ta="center" py="md">
            No providers match the current filter.
          </MantineText>
        ) : (
          filtered.map(({ v, auth, testable, languages }) => {
            const showAllLangs = languages.length <= LANGUAGE_CHIP_THRESHOLD;
            const langTooltip = languages
              .map((code) => getLanguageName(code))
              .join(", ");
            return (
              <UnstyledButton
                key={v.key}
                className={styles.providerPickerRow}
                onClick={() => onPick(v)}
              >
                <div className={styles.providerPickerHead}>
                  <span className={styles.providerPickerName}>
                    {v.name ?? capitalize(v.key)}
                  </span>
                  <Badge size="xs" variant="light">
                    {v.source === "plugin" ? "Plugin" : "Shipped"}
                  </Badge>
                </div>
                {v.description && (
                  <div className={styles.providerPickerDesc}>
                    {v.description}
                  </div>
                )}
                <div className={styles.providerPickerChips}>
                  <Badge size="xs" variant="outline" color="gray">
                    {AUTH_LABEL[auth]}
                  </Badge>
                  {testable && (
                    <Badge size="xs" variant="outline" color="gray">
                      Testable
                    </Badge>
                  )}
                  {languages.length > 0 &&
                    showAllLangs &&
                    languages.map((code) => (
                      <Badge
                        key={code}
                        size="xs"
                        variant="outline"
                        color="gray"
                      >
                        {getLanguageName(code)}
                      </Badge>
                    ))}
                  {languages.length > LANGUAGE_CHIP_THRESHOLD && (
                    <Badge
                      size="xs"
                      variant="outline"
                      color="gray"
                      title={langTooltip}
                    >
                      {languages.length} languages
                    </Badge>
                  )}
                </div>
                {v.message && (
                  <div className={styles.providerPickerNote}>
                    <FontAwesomeIcon
                      icon={faCircleInfo}
                      className={styles.providerPickerNoteIcon}
                    />
                    <span>{v.message}</span>
                  </div>
                )}
              </UnstyledButton>
            );
          })
        )}
      </div>
    </Stack>
  );
};

const validation = ProviderList.map((provider) => {
  return provider.inputs
    ?.map((input) => {
      if (input.validation === undefined) {
        return null;
      }

      return {
        [`settings-${provider.key}-${input.key}`]: input.validation?.rule,
      };
    })
    .filter((input) => input && Object.keys(input).length > 0)
    .reduce((acc, curr) => {
      return { ...acc, ...curr };
    }, {});
})
  .filter((provider) => provider && Object.keys(provider).length > 0)
  .reduce((acc, item) => {
    return { ...acc, ...item };
  }, {});

const ProviderTool: FunctionComponent<ProviderToolProps> = ({
  payload,
  enabledProviders,
  staged,
  settings,
  onChange,
  availableOptions,
  settingsKey,
  onClose,
}) => {
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  const [info, setInfo] = useState<Nullable<ProviderInfo>>(payload);
  const seededPriorities = resolveProviderPriorities(staged, settings);

  const form = useForm<FormValues>({
    initialValues: {
      settings: {
        ...staged,
        [`settings-general-provider_priorities-${info?.key}`]:
          seededPriorities[info?.key ?? ""] ?? 100,
      },
      hooks: {},
    },
    validate: {
      settings: validation!,
    },
  });

  useEffect(() => {
    if (info?.key) {
      const priorityKey = `settings-general-provider_priorities-${info.key}`;
      const priorityValue =
        resolveProviderPriorities(staged, settings)[info.key] ?? 100;
      form.setFieldValue(`settings.${priorityKey}`, priorityValue);
    }
  }, [info?.key]); // eslint-disable-line react-hooks/exhaustive-deps

  const deletePayload = useCallback(() => {
    if (payload && enabledProviders) {
      const idx = enabledProviders.findIndex((v) => v === payload.key);
      if (idx !== -1) {
        const newProviders = [...enabledProviders];
        newProviders.splice(idx, 1);

        const changes: LooseObject = { [settingsKey]: newProviders };

        const priorities = resolveProviderPriorities(staged, settings);
        const nextPriorities = { ...priorities };
        delete nextPriorities[payload.key];
        changes["settings-general-provider_priorities"] =
          JSON.stringify(nextPriorities);

        onChangeRef.current(changes);
        onClose();
      }
    }
  }, [payload, enabledProviders, onClose, settingsKey, staged, settings]);

  const submit = useCallback(
    (values: FormValues) => {
      const result = form.validate();

      if (result.hasErrors) {
        return;
      }

      if (info && enabledProviders) {
        const changes = { ...values.settings };
        const hooks = values.hooks;

        const isNewProvider =
          enabledProviders.find((v) => v === info.key) === undefined;
        if (isNewProvider) {
          changes[settingsKey] = [...enabledProviders, info.key];
        }

        const priorityKey = `settings-general-provider_priorities-${info.key}`;
        const priorities = resolveProviderPriorities(values.settings, settings);
        if (isNewProvider) {
          const maxPriority = Object.values(priorities).reduce(
            (max, v) => (typeof v === "number" && v > max ? v : max),
            0,
          );
          priorities[info.key] = maxPriority + 10;
        } else if (priorities[info.key] === undefined) {
          priorities[info.key] = 100;
        }
        changes["settings-general-provider_priorities"] =
          JSON.stringify(priorities);
        delete changes[priorityKey];

        // Apply submit hooks
        runHooks(hooks, changes);

        onChangeRef.current(changes);
        onClose();
      }
    },
    [info, enabledProviders, onClose, settingsKey, form, settings],
  );

  const canSave = info !== null;

  const onSelect = useCallback((item: Nullable<ProviderInfo>) => {
    if (item) {
      setInfo(item);
    } else {
      setInfo({
        key: "",
        description: "Unknown Provider",
      });
    }
  }, []);

  const pickerOptions = useMemo(
    () =>
      availableOptions.filter(
        (v) =>
          enabledProviders?.find((p) => p === v.key && p !== info?.key) ===
          undefined,
      ),
    [info?.key, enabledProviders, availableOptions],
  );

  const inputs = useMemo(() => {
    if (info === null || info.inputs === undefined) {
      return null;
    }

    const itemKey = info.key;

    const elements: JSX.Element[] = [];

    info.inputs?.forEach((value) => {
      const key = value.key;
      const label = value.name ?? capitalize(value.key);
      const options = value.options ?? [];

      // Check if this field should be conditionally rendered
      if (value.condition) {
        const conditionKey = `settings-${itemKey}-${value.condition.key}`;
        const conditionValue = form.values.settings[conditionKey];

        // Skip rendering if condition is not met
        if (conditionValue !== value.condition.value) {
          return;
        }
      }

      const error = form.errors[`settings.settings-${itemKey}-${key}`] ? (
        <MantineText c="red" component="span" size="xs">
          {form.errors[`settings.settings-${itemKey}-${key}`]}
        </MantineText>
      ) : null;

      switch (value.type) {
        case "text":
          elements.push(
            <Fragment key={BuildKey(itemKey, key)}>
              <Text
                label={label}
                settingKey={`settings-${itemKey}-${key}`}
              ></Text>
              {error}
            </Fragment>,
          );
          return;
        case "password":
          elements.push(
            <Fragment key={BuildKey(itemKey, key)}>
              <Password
                label={label}
                settingKey={`settings-${itemKey}-${key}`}
              ></Password>
              {error}
            </Fragment>,
          );
          return;
        case "switch":
          elements.push(
            <Fragment key={BuildKey(itemKey, key)}>
              <Check
                inline
                label={label}
                settingKey={`settings-${itemKey}-${key}`}
              ></Check>
              {error}
            </Fragment>,
          );
          return;
        case "select":
          elements.push(
            <Fragment key={BuildKey(itemKey, key)}>
              <GlobalSelector
                label={label}
                settingKey={`settings-${itemKey}-${key}`}
                options={options}
              ></GlobalSelector>
              {error}
            </Fragment>,
          );
          return;
        case "testbutton":
          elements.push(
            <ProviderTestButton category={key}></ProviderTestButton>,
          );
          return;
        case "chips":
          elements.push(
            <Fragment key={BuildKey(itemKey, key)}>
              <Chips
                label={label}
                settingKey={`settings-${itemKey}-${key}`}
              ></Chips>
              {error}
            </Fragment>,
          );
          return;
        default:
          ASSERT(false, "Implement your new input here");
      }
    });

    return <Stack gap="xs">{elements}</Stack>;
  }, [info, form, form.values.settings]); // eslint-disable-line react-hooks/exhaustive-deps

  const displayName =
    info?.name ?? (info?.key ? capitalize(info.key) : undefined);

  const isPickerMode = payload === null && info === null;

  return (
    <SettingsProvider value={settings}>
      <FormContext.Provider value={form}>
        <div className={styles.providerDrawerLayout}>
          <div
            className={
              isPickerMode
                ? `${styles.providerDrawerScroll} ${styles.providerDrawerScrollPicker}`
                : styles.providerDrawerScroll
            }
          >
            <Stack gap="lg" className={styles.providerDrawerSections}>
              {payload === null ? (
                info === null ? (
                  <Stack
                    gap="xs"
                    className={styles.providerDrawerPickerSection}
                  >
                    <MantineText size="sm" fw={600}>
                      Choose a provider
                    </MantineText>
                    <MantineText size="xs" c="dimmed">
                      Pick a shipped provider or an active installed plugin.
                      Installed plugins are enabled for searches only after you
                      add them here and save settings.
                    </MantineText>
                    <ProviderPicker options={pickerOptions} onPick={onSelect} />
                  </Stack>
                ) : (
                  <Stack gap={4}>
                    <Group gap="xs" justify="space-between" align="center">
                      <Group gap="xs" align="center">
                        <MantineText size="sm" fw={600}>
                          {displayName ?? info.key}
                        </MantineText>
                        {info.source === "plugin" && (
                          <Badge size="xs" variant="light">
                            Plugin
                          </Badge>
                        )}
                      </Group>
                      <Button
                        variant="subtle"
                        size="xs"
                        leftSection={<FontAwesomeIcon icon={faArrowLeft} />}
                        onClick={() => setInfo(null)}
                      >
                        Change
                      </Button>
                    </Group>
                    {info.description && (
                      <MantineText size="xs" c="dimmed">
                        {info.description}
                      </MantineText>
                    )}
                  </Stack>
                )
              ) : (
                displayName && (
                  <Stack gap={4}>
                    <Group gap="xs" align="center">
                      <MantineText size="sm" fw={600}>
                        {displayName}
                      </MantineText>
                      {info?.source === "plugin" && (
                        <Badge size="xs" variant="light">
                          Plugin
                        </Badge>
                      )}
                    </Group>
                    {info?.description && (
                      <MantineText size="xs" c="dimmed">
                        {info.description}
                      </MantineText>
                    )}
                  </Stack>
                )
              )}

              {info?.inputs && info.inputs.length > 0 && (
                <Stack gap="xs">
                  <MantineText size="sm" fw={600}>
                    Credentials &amp; settings
                  </MantineText>
                  {inputs}
                </Stack>
              )}

              {info?.message && (
                <Stack gap="xs">
                  <MantineText size="sm" fw={600}>
                    Notes
                  </MantineText>
                  <MantineText size="xs" c="dimmed">
                    {info.message}
                  </MantineText>
                </Stack>
              )}
            </Stack>
          </div>

          <div className={styles.providerDrawerActions}>
            {payload ? (
              <Button color="red" variant="subtle" onClick={deletePayload}>
                Remove
              </Button>
            ) : (
              <span />
            )}
            <Group gap="xs">
              <Button variant="default" onClick={onClose}>
                Cancel
              </Button>
              <Button
                disabled={!canSave}
                onClick={() => {
                  submit(form.values);
                }}
              >
                Save
              </Button>
            </Group>
          </div>
        </div>
      </FormContext.Provider>
    </SettingsProvider>
  );
};
