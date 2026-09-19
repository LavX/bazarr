import { FunctionComponent, useCallback, useState } from "react";
import { Autocomplete, Text as MantineText } from "@mantine/core";
import { useFormActions } from "@/pages/Settings/utilities/FormValues";
import {
  useBaseInput,
  useSettingValue,
} from "@/pages/Settings/utilities/hooks";
import { aiTranslatorModelOptions } from "./options";
import { routingLabel, splitRoutingSuffix } from "./routing";

const modelData = aiTranslatorModelOptions.map((o) => o.value);

const ROUTING_KEY = "settings-translator-openrouter_provider_routing";

const AIModelSelector: FunctionComponent = () => {
  const { value, update } = useBaseInput<{ settingKey: string }, string>({
    settingKey: "settings-translator-openrouter_model",
  });
  const { setValue } = useFormActions();
  const [adopted, setAdopted] = useState<string | null>(null);
  const [declined, setDeclined] = useState<string | null>(null);
  const [edited, setEdited] = useState(false);
  // The selector that owns the routing lives elsewhere on the page, so the
  // notice below follows the setting rather than asserting what it once was.
  const routing = useSettingValue<string>(ROUTING_KEY);

  // :nitro, :floor and :smartfast are routing shortcuts rather than models, so a
  // model id carrying one is split: the model keeps its own id, and the routing
  // moves to the selector that owns it. Leaving both in place would send a slug
  // and a sort that disagree, and would stop the model details lookup from
  // resolving the id. (:nitro and :floor are OpenRouter's own; :smartfast belongs
  // to AI Subtitle Translator, which strips it before talking to OpenRouter.)
  //
  // This waits for the field to be left rather than running on each keystroke.
  // Mid-word the text can be an exact suffix of something longer, so adopting
  // as the user types would turn "some/model:floorplan" into "some/modelplan".
  const adopt = useCallback(() => {
    // Only a field the user edited is adopted. Blur fires when they merely click away
    // from a field they were reading, and rewriting a stored id there staged changes
    // they never made, which the Ctrl+S shortcut then submitted for them.
    if (!edited) {
      return;
    }
    const raw = value ?? "";
    const { modelId, routing: typed } = splitRoutingSuffix(raw);
    // The details lookup works on the normalised id, so the setting has to hold
    // the same thing. Otherwise a padded id shows a valid model card while the
    // translation request goes out with the padding still on the slug.
    if (modelId !== raw) {
      update(modelId);
    }
    if (!typed) {
      setEdited(false);
      return;
    }
    // An explicit SmartFast or Custom selection wins over a shortcut typed here, which
    // is exactly how the backend resolves the same pair: it keeps the selected sort and
    // only strips the shortcut off the id. Custom additionally names providers that
    // switching away would orphan without ever saying so.
    if (routing === "custom" || routing === "smartfast") {
      setAdopted(null);
      setDeclined(typed);
      setEdited(false);
      return;
    }
    setDeclined(null);
    setValue(typed, ROUTING_KEY);
    setAdopted(typed);
    setEdited(false);
  }, [value, update, setValue, routing, edited]);

  const onChange = useCallback(
    (raw: string) => {
      update(raw);
      setAdopted(null);
      setDeclined(null);
      setEdited(true);
    },
    [update],
  );

  return (
    <>
      <Autocomplete
        label="AI Model"
        data={modelData}
        value={(value as string) ?? ""}
        onChange={onChange}
        onBlur={adopt}
        placeholder="Select or type any model ID..."
        limit={30}
      />
      {adopted !== null && adopted === routing && (
        <MantineText size="xs" c="yellow.6" mt={4}>
          Moved :{adopted} into Provider Routing, now set to{" "}
          {routingLabel(adopted)}.
        </MantineText>
      )}
      {declined !== null &&
        (routing === "custom" || routing === "smartfast") && (
          <MantineText size="xs" c="yellow.6" mt={4}>
            Removed :{declined} from the model id. Provider Routing stays{" "}
            {routingLabel(routing)}. Switch it to {routingLabel(declined)} if
            you want that instead.
          </MantineText>
        )}
    </>
  );
};

export default AIModelSelector;
