import React, { FunctionComponent, useCallback, useMemo } from "react";
import {
  Accordion,
  Button,
  Checkbox,
  Divider,
  Flex,
  Group,
  Radio,
  Select,
  Stack,
  Switch,
  Text,
  TextInput,
  Tooltip,
} from "@mantine/core";
import { useForm } from "@mantine/form";
import { faTrash } from "@fortawesome/free-solid-svg-icons";
import { ColumnDef } from "@tanstack/react-table";
import { Action, Selector, SelectorOption } from "@/components";
import ChipInput from "@/components/inputs/ChipInput";
import SimpleTable from "@/components/tables/SimpleTable";
import { useModals, withModal } from "@/modules/modals";
import { useArrayAction, useSelectorOptions } from "@/utilities";
import { LOG } from "@/utilities/console";
import FormUtils from "@/utilities/form";
import styles from "./ProfileEditForm.module.scss";

export const anyCutoff = 65535;

const defaultCutoffOptions: SelectorOption<Language.ProfileItem>[] = [
  {
    label: "Any",
    value: {
      id: anyCutoff,
      // eslint-disable-next-line camelcase
      audio_exclude: "False",
      // eslint-disable-next-line camelcase
      audio_only_include: "False",
      forced: "False",
      hi: "False",
      language: "any",
      // eslint-disable-next-line camelcase
      translate_from: null,
    },
  },
];

const subtitlesTypeOptions: SelectorOption<string>[] = [
  {
    label: "Normal or hearing-impaired",
    value: "normal",
  },
  {
    label: "Hearing-impaired required",
    value: "hi",
  },
  {
    label: "Forced (foreign part only)",
    value: "forced",
  },
];

const inclusionOptions: SelectorOption<string>[] = [
  {
    label: "Always",
    value: "always_include",
  },
  {
    label: "audio track matches",
    value: "audio_only_include",
  },
  {
    label: "no audio track matches",
    value: "audio_exclude",
  },
];

interface CombineRuleEditorProps {
  items: Language.ProfileItem[];
  value: Language.CombineRule | null | undefined;
  onChange: (next: Language.CombineRule | null) => void;
}

const CombineRuleEditor: FunctionComponent<CombineRuleEditorProps> = ({
  items,
  value,
  onChange,
}) => {
  const enabled = value != null;
  const itemCodes = useMemo(
    // Deduplicate base language codes: a profile with normal + forced/HI English
    // yields ['en','en'], and seeding a combine rule with duplicate languages is
    // rejected by validate_combine_rule, leaving the rule inert (no auto-combine).
    () => [...new Set(items.map((it) => it.language).filter(Boolean))],
    [items],
  );

  const setLanguageAt = (idx: number, code: string | null) => {
    if (!value) return;
    const next = [...value.languages];
    if (code) {
      next[idx] = code;
    } else {
      next.splice(idx, 1);
    }
    onChange({ ...value, languages: next });
  };

  return (
    <Stack>
      <Divider
        label="Combined subtitle output (optional)"
        labelPosition="left"
      />
      <Checkbox
        label="Generate a combined subtitle file when all selected languages are present"
        checked={enabled}
        onChange={(e) => {
          if (e.currentTarget.checked) {
            const seed = itemCodes.slice(0, 2);
            if (seed.length === 2) {
              onChange({ languages: seed, format: "srt" });
            }
          } else {
            onChange(null);
          }
        }}
      />
      {enabled && value && (
        <>
          <Text size="sm">Languages (in display order, max 3)</Text>
          <Group>
            {value.languages.map((code, idx) => (
              <Select
                key={`${code}-${idx}`}
                value={code}
                data={itemCodes.map((c) => ({
                  value: c,
                  label: c.toUpperCase(),
                }))}
                onChange={(next) => setLanguageAt(idx, next)}
                style={{ width: 120 }}
              />
            ))}
            {value.languages.length < 3 &&
              itemCodes.length > value.languages.length && (
                <Select
                  placeholder="Add language"
                  data={itemCodes
                    .filter((c) => !value.languages.includes(c))
                    .map((c) => ({ value: c, label: c.toUpperCase() }))}
                  onChange={(next) => {
                    if (next) {
                      onChange({
                        ...value,
                        languages: [...value.languages, next],
                      });
                    }
                  }}
                  style={{ width: 140 }}
                />
              )}
          </Group>
          <Radio.Group
            label="Output format"
            value={value.format}
            onChange={(f) => onChange({ ...value, format: f as "srt" | "ass" })}
          >
            <Group mt="xs">
              <Radio value="srt" label="SRT stacked" />
              <Radio value="ass" label="ASS positioned" />
            </Group>
          </Radio.Group>
          <Tooltip
            multiline
            w={320}
            label="Bazarr writes one extra file when all selected languages have a subtitle on disk for this video. This feature only composes existing files; it never triggers translation."
          >
            <Text size="xs" c="dimmed">
              How does this work?
            </Text>
          </Tooltip>
        </>
      )}
    </Stack>
  );
};

interface Props {
  onComplete?: (profile: Language.Profile) => void;
  languages: readonly Language.Info[];
  profile: Language.Profile;
}

const ProfileEditForm: FunctionComponent<Props> = ({
  onComplete,
  languages,
  profile,
}) => {
  const modals = useModals();

  const form = useForm({
    initialValues: profile,
    validate: {
      name: FormUtils.validation(
        (value: string) => value.length > 0,
        "Must have a name",
      ),
      tag: FormUtils.validation((value: string | undefined) => {
        if (!value) {
          return true;
        }

        return /^[a-z_0-9-]+$/.test(value);
      }, "Only lowercase alphanumeric characters, underscores (_) and hyphens (-) are allowed"),
      items: FormUtils.validation(
        (value: Language.ProfileItem[]) => value.length > 0,
        "Must contain at least 1 language",
      ),
    },
  });

  const languageOptions = useSelectorOptions(languages, (l) => l.name);

  const itemCutoffOptions = useSelectorOptions(
    form.values.items,
    (v) => {
      const suffix =
        v.hi === "True" ? ":hi" : v.forced === "True" ? ":forced" : "";

      return v.language + suffix;
    },
    (v) => String(v.id),
  );

  const cutoffOptions = useMemo(
    () => ({
      ...itemCutoffOptions,
      options: [...itemCutoffOptions.options, ...defaultCutoffOptions],
    }),
    [itemCutoffOptions],
  );

  const selectedCutoff = useMemo(
    () =>
      cutoffOptions.options.find((v) => v.value.id === form.values.cutoff)
        ?.value ?? null,
    [cutoffOptions, form.values.cutoff],
  );

  const mustContainOptions = useSelectorOptions(
    form.values.mustContain,
    (v) => v,
  );

  const mustNotContainOptions = useSelectorOptions(
    form.values.mustNotContain,
    (v) => v,
  );

  const action = useArrayAction<Language.ProfileItem>((fn) => {
    form.setValues((values) => ({ ...values, items: fn(values.items ?? []) }));
  });

  const addItem = useCallback(() => {
    const id =
      1 +
      form.values.items.reduce<number>(
        (val, item) => Math.max(item.id, val),
        0,
      );

    if (languages.length > 0) {
      const language = languages[0].code2;

      const item: Language.ProfileItem = {
        id,
        language,
        // eslint-disable-next-line camelcase
        audio_exclude: "False",
        // eslint-disable-next-line camelcase
        audio_only_include: "False",
        hi: "False",
        forced: "False",
        // eslint-disable-next-line camelcase
        translate_from: null,
      };

      const list = [...form.values.items, item];
      form.setValues((values) => ({ ...values, items: list }));
    }
  }, [form, languages]);

  const LanguageCell = React.memo(
    ({ item, index }: { item: Language.ProfileItem; index: number }) => {
      const code = useMemo(
        () =>
          languageOptions.options.find((l) => l.value.code2 === item.language)
            ?.value ?? null,
        [item.language],
      );

      return (
        <Selector
          {...languageOptions}
          className="table-select"
          value={code}
          onChange={(value) => {
            if (value) {
              action.mutate(index, { ...item, language: value.code2 });
            }
          }}
        ></Selector>
      );
    },
  );

  const SubtitleTypeCell = React.memo(
    ({ item, index }: { item: Language.ProfileItem; index: number }) => {
      const selectValue = useMemo(() => {
        if (item.forced === "True") {
          return "forced";
        } else if (item.hi === "True") {
          return "hi";
        } else {
          return "normal";
        }
      }, [item.forced, item.hi]);

      return (
        <Select
          value={selectValue}
          data={subtitlesTypeOptions}
          onChange={(value) => {
            if (value) {
              action.mutate(index, {
                ...item,
                hi: value === "hi" ? "True" : "False",
                forced: value === "forced" ? "True" : "False",
              });
            }
          }}
        ></Select>
      );
    },
  );

  const InclusionCell = React.memo(
    ({ item, index }: { item: Language.ProfileItem; index: number }) => {
      const selectValue = useMemo(() => {
        if (item.audio_exclude === "True") {
          return "audio_exclude";
        } else if (item.audio_only_include === "True") {
          return "audio_only_include";
        } else {
          return "always_include";
        }
      }, [item.audio_exclude, item.audio_only_include]);

      return (
        <Select
          value={selectValue}
          data={inclusionOptions}
          onChange={(value) => {
            if (value) {
              action.mutate(index, {
                ...item,
                // eslint-disable-next-line camelcase
                audio_exclude: value === "audio_exclude" ? "True" : "False",
                // eslint-disable-next-line camelcase
                audio_only_include:
                  value === "audio_only_include" ? "True" : "False",
              });
            }
          }}
        ></Select>
      );
    },
  );

  const TranslateFromCell = React.memo(
    ({ item, index }: { item: Language.ProfileItem; index: number }) => {
      // Exclude the item's own language from the source list (can't translate
      // a language to itself). Treat undefined translate_from as null for
      // backward compatibility with profile rows persisted before this field.
      const translateFromValue = item.translate_from ?? null;

      const filteredOptions = useMemo(
        () =>
          languageOptions.options.filter(
            (l) => l.value.code2 !== item.language,
          ),
        [item.language],
      );

      const selected = useMemo(
        () =>
          filteredOptions.find((l) => l.value.code2 === translateFromValue)
            ?.value ?? null,
        [filteredOptions, translateFromValue],
      );

      return (
        <Selector
          {...languageOptions}
          options={filteredOptions}
          className="table-select"
          clearable
          placeholder="None"
          value={selected}
          onChange={(value) => {
            action.mutate(index, {
              ...item,
              // eslint-disable-next-line camelcase
              translate_from: value?.code2 ?? null,
            });
          }}
        ></Selector>
      );
    },
  );

  const columns = useMemo<ColumnDef<Language.ProfileItem>[]>(
    () => [
      {
        header: "ID",
        accessorKey: "id",
      },
      {
        header: "Language",
        accessorKey: "language",
        cell: ({ row: { original: item, index } }) => {
          return <LanguageCell item={item} index={index} />;
        },
      },
      {
        header: "Subtitles Type",
        accessorKey: "forced",
        cell: ({ row: { original: item, index } }) => {
          return <SubtitleTypeCell item={item} index={index} />;
        },
      },
      {
        header: "Search only when...",
        accessorKey: "audio_exclude",
        cell: ({ row: { original: item, index } }) => {
          return <InclusionCell item={item} index={index} />;
        },
      },
      {
        header: "Translate From",
        accessorKey: "translate_from",
        cell: ({ row: { original: item, index } }) => {
          return <TranslateFromCell item={item} index={index} />;
        },
      },
      {
        id: "action",
        cell: ({ row }) => {
          return (
            <Action
              label="Remove"
              icon={faTrash}
              c="red"
              onClick={() => action.remove(row.index)}
            ></Action>
          );
        },
      },
    ],
    [action, LanguageCell, SubtitleTypeCell, InclusionCell, TranslateFromCell],
  );

  return (
    <form
      onSubmit={form.onSubmit((value) => {
        LOG("info", "Submitting language profile", value);
        onComplete?.(value);
        modals.closeSelf();
      })}
    >
      <Stack>
        <Flex
          direction={{ base: "column", sm: "row" }}
          gap="sm"
          className={styles.evenly}
        >
          <TextInput label="Name" {...form.getInputProps("name")}></TextInput>
          <TextInput
            label="Tag"
            {...form.getInputProps("tag")}
            onBlur={() =>
              form.setFieldValue(
                "tag",
                (prev) =>
                  prev?.toLowerCase().trim().replace(/\s+/g, "_") ?? undefined,
              )
            }
          ></TextInput>
        </Flex>
        <Accordion
          multiple
          chevronPosition="right"
          defaultValue={["Languages"]}
          className={styles.content}
        >
          <Accordion.Item value="Languages">
            <Stack>
              <SimpleTable
                columns={columns}
                data={form.values.items}
              ></SimpleTable>
              <Button fullWidth onClick={addItem}>
                Add Language
              </Button>
              <Text c="var(--mantine-color-error)">{form.errors.items}</Text>
              <Selector
                clearable
                label="Cutoff"
                {...cutoffOptions}
                value={selectedCutoff}
                onChange={(value) => {
                  form.setFieldValue("cutoff", value?.id ?? null);
                }}
              ></Selector>
            </Stack>
          </Accordion.Item>
          <Accordion.Item value="Release Info">
            <Stack>
              <ChipInput
                label="Must contain"
                {...mustContainOptions}
                {...form.getInputProps("mustContain")}
              ></ChipInput>
              <Text size="sm">
                Subtitles release info must include one of those words or they
                will be excluded from search results (regex supported).
              </Text>
              <ChipInput
                label="Must not contain"
                {...mustNotContainOptions}
                {...form.getInputProps("mustNotContain")}
              ></ChipInput>
              <Text size="sm">
                Subtitles release info including one of those words (case
                insensitive) will be excluded from search results (regex
                supported).
              </Text>
            </Stack>
          </Accordion.Item>
          <Accordion.Item value="Subtitles">
            <Stack my="xs">
              <Switch
                label="Use Original Format"
                checked={form.values.originalFormat ?? false}
                {...form.getInputProps("originalFormat")}
              ></Switch>
              <Text size="sm">
                Download subtitle file without format conversion
              </Text>
            </Stack>
          </Accordion.Item>
        </Accordion>
        <CombineRuleEditor
          items={form.values.items}
          value={form.values.combine ?? null}
          onChange={(next) => form.setFieldValue("combine", next)}
        />
        <Button type="submit">Save</Button>
      </Stack>
    </form>
  );
};

export const ProfileEditModal = withModal(
  ProfileEditForm,
  "languages-profile-editor",
  {
    title: "Edit Languages Profile",
    size: "xl",
  },
);
