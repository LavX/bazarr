import { FunctionComponent } from "react";
import { Autocomplete } from "@mantine/core";
import { useBaseInput } from "@/pages/Settings/utilities/hooks";
import { aiTranslatorModelOptions } from "./options";

const modelData = aiTranslatorModelOptions.map((o) => o.value);

const AIModelSelector: FunctionComponent = () => {
  const { value, update } = useBaseInput<{ settingKey: string }, string>({
    settingKey: "settings-translator-openrouter_model",
  });

  return (
    <Autocomplete
      label="AI Model"
      data={modelData}
      value={(value as string) ?? ""}
      onChange={(val) => update(val)}
      placeholder="Select or type any model ID..."
      limit={30}
    />
  );
};

export default AIModelSelector;
