import { FunctionComponent, useMemo } from "react";
import {
  Alert,
  Badge,
  Button,
  Divider,
  Group,
  Stack,
  Text,
} from "@mantine/core";
import { useForm } from "@mantine/form";
import { notifications } from "@mantine/notifications";
import { isObject } from "lodash";
import { useBatchAction, useSystemSettings } from "@/apis/hooks";
import { BatchItem } from "@/apis/raw/subtitles";
import { Selector } from "@/components/inputs";
import { useModals, withModal } from "@/modules/modals";
import { useSelectorOptions } from "@/utilities";
import FormUtils from "@/utilities/form";
import { useEnabledLanguages } from "@/utilities/languages";

// Translations map for Google Translate compatibility
const googleTranslations: Record<string, string> = {
  af: "afrikaans",
  sq: "albanian",
  am: "amharic",
  ar: "arabic",
  hy: "armenian",
  az: "azerbaijani",
  eu: "basque",
  be: "belarusian",
  bn: "bengali",
  bs: "bosnian",
  bg: "bulgarian",
  ca: "catalan",
  ceb: "cebuano",
  ny: "chichewa",
  zh: "chinese (simplified)",
  zt: "chinese (traditional)",
  co: "corsican",
  hr: "croatian",
  cs: "czech",
  da: "danish",
  nl: "dutch",
  en: "english",
  eo: "esperanto",
  et: "estonian",
  tl: "filipino",
  fi: "finnish",
  fr: "french",
  fy: "frisian",
  gl: "galician",
  ka: "georgian",
  de: "german",
  el: "greek",
  gu: "gujarati",
  ht: "haitian creole",
  ha: "hausa",
  haw: "hawaiian",
  iw: "hebrew",
  hi: "hindi",
  hmn: "hmong",
  hu: "hungarian",
  is: "icelandic",
  ig: "igbo",
  id: "indonesian",
  ga: "irish",
  it: "italian",
  ja: "japanese",
  jw: "javanese",
  kn: "kannada",
  kk: "kazakh",
  km: "khmer",
  ko: "korean",
  ku: "kurdish (kurmanji)",
  ky: "kyrgyz",
  lo: "lao",
  la: "latin",
  lv: "latvian",
  lt: "lithuanian",
  lb: "luxembourgish",
  mk: "macedonian",
  mg: "malagasy",
  ms: "malay",
  ml: "malayalam",
  mt: "maltese",
  mi: "maori",
  mr: "marathi",
  mn: "mongolian",
  my: "myanmar (burmese)",
  ne: "nepali",
  no: "norwegian",
  ps: "pashto",
  fa: "persian",
  pl: "polish",
  pt: "portuguese",
  pa: "punjabi",
  ro: "romanian",
  ru: "russian",
  sm: "samoan",
  gd: "scots gaelic",
  sr: "serbian",
  st: "sesotho",
  sn: "shona",
  sd: "sindhi",
  si: "sinhala",
  sk: "slovak",
  sl: "slovenian",
  so: "somali",
  es: "spanish",
  su: "sundanese",
  sw: "swahili",
  sv: "swedish",
  tg: "tajik",
  ta: "tamil",
  te: "telugu",
  th: "thai",
  tr: "turkish",
  uk: "ukrainian",
  ur: "urdu",
  uz: "uzbek",
  vi: "vietnamese",
  cy: "welsh",
  xh: "xhosa",
  yi: "yiddish",
  yo: "yoruba",
  zu: "zulu",
  fil: "Filipino",
  he: "Hebrew",
};

export interface WantedEpisodeItem {
  type: "episode";
  sonarrSeriesId: number;
  sonarrEpisodeId: number;
  seriesTitle: string;
  episodeTitle: string;
}

export interface WantedMovieItem {
  type: "movie";
  radarrId: number;
  title: string;
}

export interface WantedSeriesItem {
  type: "series";
  sonarrSeriesId: number;
  title: string;
}

export type WantedItem = WantedEpisodeItem | WantedMovieItem | WantedSeriesItem;

interface Props {
  items: WantedItem[];
  onComplete?: () => void;
}

interface TranslationConfig {
  service: string;
  model: string;
}

const MassTranslateForm: FunctionComponent<Props> = ({ items, onComplete }) => {
  const settings = useSystemSettings();
  const { mutateAsync, isPending } = useBatchAction();
  const modals = useModals();

  const { data: languages } = useEnabledLanguages();

  const form = useForm({
    initialValues: {
      sourceLanguage: null as Language.Info | null,
      targetLanguage: null as Language.Info | null,
    },
    validate: {
      sourceLanguage: FormUtils.validation(
        isObject,
        "Please select a source language",
      ),
      targetLanguage: (
        value: Language.Info | null,
        values: { sourceLanguage: Language.Info | null },
      ) => {
        if (!isObject(value)) return "Please select a target language";
        if (
          value &&
          values.sourceLanguage &&
          value.code2 === values.sourceLanguage.code2
        )
          return "Target language must be different from source";
        return null;
      },
    },
  });

  const translatorType = settings?.data?.translator?.translator_type;
  const isGoogleTranslator = translatorType === "google_translate";

  const availableLanguages = useMemo(() => {
    if (isGoogleTranslator) {
      return languages.filter((v) => v.code2 in googleTranslations);
    }
    return languages;
  }, [languages, isGoogleTranslator]);

  const sourceOptions = useSelectorOptions(
    availableLanguages,
    (v) => v.name,
    (v) => v.code2,
  );

  const targetOptions = useSelectorOptions(
    availableLanguages,
    (v) => v.name,
    (v) => v.code2,
  );

  const getTranslationConfig = (
    settingsData: ReturnType<typeof useSystemSettings>,
  ): TranslationConfig => {
    const tType = settingsData?.data?.translator?.translator_type;
    const defaultConfig: TranslationConfig = {
      service: "Google Translate",
      model: "",
    };

    switch (tType) {
      case "gemini":
        return {
          ...defaultConfig,
          service: "Gemini",
          model: ` (${settingsData?.data?.translator?.gemini_model || ""})`,
        };
      case "lingarr":
        return {
          ...defaultConfig,
          service: "Lingarr",
        };
      case "openrouter":
        return {
          ...defaultConfig,
          service: "AI Subtitle Translator",
          model: ` (${settingsData?.data?.translator?.openrouter_model || ""})`,
        };
      default:
        return defaultConfig;
    }
  };

  const config = getTranslationConfig(settings);
  const translatorService = config.service;
  const translatorModel = config.model;

  const handleSubmit = async (values: {
    sourceLanguage: Language.Info | null;
    targetLanguage: Language.Info | null;
  }) => {
    if (!values.sourceLanguage || !values.targetLanguage) return;

    if (items.length >= 100) {
      const confirmed = window.confirm(
        `This will translate subtitles for ${items.length} items. This may take a while. Continue?`,
      );
      if (!confirmed) return;
    }

    const batchItems: BatchItem[] = items.map((item) => {
      if (item.type === "episode") {
        return {
          type: "episode" as const,
          sonarrSeriesId: item.sonarrSeriesId,
          sonarrEpisodeId: item.sonarrEpisodeId,
        };
      } else if (item.type === "series") {
        return {
          type: "series" as const,
          sonarrSeriesId: item.sonarrSeriesId,
        };
      } else {
        return {
          type: "movie" as const,
          radarrId: item.radarrId,
        };
      }
    });

    try {
      const result = await mutateAsync({
        items: batchItems,
        action: "translate",
        options: {
          fromLang: values.sourceLanguage!.code2,
          toLang: values.targetLanguage!.code2,
        },
      });

      notifications.show({
        title: "Translation Queued",
        message: `Queued: ${result.queued}, Skipped: ${result.skipped}${result.errors.length > 0 ? `, Errors: ${result.errors.length}` : ""}`,
        color: result.errors.length > 0 ? "yellow" : "green",
      });

      onComplete?.();
      modals.closeSelf();
    } catch (error) {
      notifications.show({
        title: "Translation Failed",
        message: String(error),
        color: "red",
      });
    }
  };

  return (
    <form onSubmit={form.onSubmit(handleSubmit)}>
      <Stack>
        <Alert>
          <Text size="sm">
            {translatorService}
            {translatorModel} will be used to translate{" "}
            <strong>{items.length}</strong> item(s).
          </Text>
          <Text size="xs" c="var(--bz-text-tertiary)" mt="xs">
            You can choose translation service in the subtitles settings.
          </Text>
        </Alert>

        {items.length > 0 && items.length <= 5 && (
          <Stack gap="xs">
            <Text size="sm" fw={500}>
              Selected items:
            </Text>
            <Group gap="xs">
              {items.map((item, idx) => (
                <Badge key={idx} variant="light" size="sm">
                  {item.type === "episode"
                    ? `${item.seriesTitle} - ${item.episodeTitle}`
                    : item.title}
                </Badge>
              ))}
            </Group>
          </Stack>
        )}

        {items.length > 5 && (
          <Text size="sm" c="var(--bz-text-tertiary)">
            {items.length} items selected for translation
          </Text>
        )}

        {isGoogleTranslator && (
          <Alert variant="outline" color="yellow">
            <Text size="xs">
              Enabled languages not listed here are unsupported by{" "}
              {translatorService}.
            </Text>
          </Alert>
        )}

        <Selector
          label="Source Language"
          placeholder="Select source language (e.g., English)"
          {...sourceOptions}
          {...form.getInputProps("sourceLanguage")}
        />

        <Selector
          label="Target Language"
          placeholder="Select target language"
          {...targetOptions}
          {...form.getInputProps("targetLanguage")}
        />

        <Divider />

        <Alert variant="light" color="blue">
          <Text size="xs">
            Each item must have an existing subtitle in the source language.
            Items without matching subtitles will be skipped.
          </Text>
        </Alert>

        <Group justify="space-between">
          <Button variant="default" onClick={() => modals.closeSelf()}>
            Cancel
          </Button>
          <Button
            type="submit"
            loading={isPending}
            disabled={items.length === 0}
          >
            Translate {items.length} Item(s)
          </Button>
        </Group>
      </Stack>
    </form>
  );
};

export const MassTranslateModal = withModal(
  MassTranslateForm,
  "mass-translate",
  {
    title: "Mass Translate Subtitles",
    size: "md",
  },
);

export default MassTranslateForm;
