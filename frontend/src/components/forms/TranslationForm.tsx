import { FunctionComponent, useMemo } from "react";
import { Alert, Button, Divider, LoadingOverlay, Stack } from "@mantine/core";
import { useForm } from "@mantine/form";
import { showNotification } from "@mantine/notifications";
import { isObject } from "lodash";
import {
  useSubtitleAction,
  useSystemSettings,
  useTranslationAvailability,
  useTranslationRefusal,
} from "@/apis/hooks";
import { Selector } from "@/components/inputs";
import { useModals, withModal } from "@/modules/modals";
import { notification } from "@/modules/notification";
import { useSelectorOptions } from "@/utilities";
import FormUtils from "@/utilities/form";
import { useEnabledLanguages } from "@/utilities/languages";

const translations = {
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

interface Props {
  selections: FormType.ModifySubtitle[];
  onSubmit?: VoidFunction;
}

interface TranslationConfig {
  service: string;
  model: string;
}

const TranslationForm: FunctionComponent<Props> = ({
  selections,
  onSubmit,
}) => {
  const settings = useSystemSettings();
  const { mutateAsync, isPending } = useSubtitleAction();
  const modals = useModals();
  const translation = useTranslationAvailability();
  // The availability above is a cached status, so the gate can have closed
  // since it was read: a 409 is where the server's reason gets a say.
  const showTranslationRefusal = useTranslationRefusal();

  const { data: languages } = useEnabledLanguages();

  const form = useForm({
    initialValues: {
      language: null as Language.Info | null,
    },
    validate: {
      language: FormUtils.validation(isObject, "Please select a language"),
    },
  });

  const translatorType = settings?.data?.translator?.translator_type;
  const isGoogleTranslator = translatorType === "google_translate";

  const available = useMemo(() => {
    // Only filter by translations if using Google Translate
    if (isGoogleTranslator) {
      return languages.filter((v) => v.code2 in translations);
    }
    // For other translators, return all enabled languages
    return languages;
  }, [languages, isGoogleTranslator]);

  const options = useSelectorOptions(
    available,
    (v) => v.name,
    (v) => v.code2,
  );

  const getTranslationConfig = (
    settings: ReturnType<typeof useSystemSettings>,
  ): TranslationConfig => {
    const translatorType = settings?.data?.translator?.translator_type;

    switch (translatorType) {
      case "google_translate":
        return { service: "Google Translate", model: "" };
      case "gemini":
        return {
          service: "Gemini",
          model: ` (${settings?.data?.translator?.gemini_model || ""})`,
        };
      case "lingarr":
        return { service: "Lingarr", model: "" };
      case "openrouter":
        return {
          service: "AI Translator",
          model: ` (${settings?.data?.translator?.openrouter_model || ""})`,
        };
      default:
        // An engine the form does not recognise is not "Google Translate":
        // it is whatever the settings page holds, and the gate below is what
        // says whether it can translate at all.
        return { service: "", model: "" };
    }
  };

  const config = getTranslationConfig(settings);
  const translatorService = config.service;
  const translatorModel = config.model;

  return (
    <form
      onSubmit={form.onSubmit(async ({ language }) => {
        if (language) {
          try {
            await Promise.all(
              selections.map((s) =>
                mutateAsync({
                  action: "translate",
                  form: {
                    ...s,
                    language: language.code2,
                    // eslint-disable-next-line camelcase
                    arr_instance_id: s.arr_instance_id,
                  },
                }),
              ),
            );
            showNotification(
              notification.info(
                "Translation started",
                `${selections.length} subtitle(s) submitted for translation`,
              ),
            );
            onSubmit?.();
            modals.closeSelf();
          } catch (error) {
            // A stale status can leave the action enabled while the gate is
            // closed: a 409 shows the server's own reason, and the generic
            // sentence stays for every other failure.
            if (!showTranslationRefusal(error)) {
              showNotification(
                notification.error(
                  "Translation failed",
                  "An error occurred while translating subtitles",
                ),
              );
            }
          }
        }
      })}
    >
      <Stack pos="relative">
        <LoadingOverlay visible={isPending} />
        {translation.available ? (
          <Alert>
            <div>
              {translatorService}
              {translatorModel} will be used.
            </div>
            <div>
              You can choose translation service in the subtitles settings.
            </div>
          </Alert>
        ) : (
          <Alert color="red">{translation.reason}</Alert>
        )}
        {isGoogleTranslator && (
          <Alert variant="outline">
            Enabled languages not listed here are unsupported by{" "}
            {translatorService}.
          </Alert>
        )}
        <Selector {...options} {...form.getInputProps("language")}></Selector>
        <Divider></Divider>
        <Button
          type="submit"
          loading={isPending}
          disabled={!translation.available}
        >
          Start
        </Button>
      </Stack>
    </form>
  );
};

export const TranslationModal = withModal(TranslationForm, "translation-tool", {
  title: "Translate Subtitle(s)",
});

export default TranslationForm;
