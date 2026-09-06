import { FunctionComponent, useCallback, useState } from "react";
import { Autocomplete, Text as MantineText } from "@mantine/core";
import { useFormActions } from "@/pages/Settings/utilities/FormValues";
import { useBaseInput } from "@/pages/Settings/utilities/hooks";
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

  // OpenRouter's :nitro and :floor are routing shortcuts rather than models, so
  // a model id carrying one is split: the model keeps its own id, and the
  // routing moves to the selector that owns it. Leaving both in place would
  // send a slug and a sort that disagree, and would stop the model details
  // lookup from resolving the id.
  //
  // This waits for the field to be left rather than running on each keystroke.
  // Mid-word the text can be an exact suffix of something longer, so adopting
  // as the user types would turn "some/model:floorplan" into "some/modelplan".
  const adopt = useCallback(() => {
    const { modelId, routing } = splitRoutingSuffix(value ?? "");
    if (!routing) {
      return;
    }
    update(modelId);
    setValue(routing, ROUTING_KEY);
    setAdopted(routing);
  }, [value, update, setValue]);

  const onChange = useCallback(
    (raw: string) => {
      update(raw);
      setAdopted(null);
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
      {adopted !== null && (
        <MantineText size="xs" c="yellow.6" mt={4}>
          Moved :{adopted} into Provider Routing, now set to{" "}
          {routingLabel(adopted)}.
        </MantineText>
      )}
    </>
  );
};

export default AIModelSelector;
