import type {
  PluginChoice,
  PluginField,
  PluginItem,
  PluginValue,
  PluginValues,
} from "./types";

export function choiceOf(item: string | PluginChoice): PluginChoice {
  return typeof item === "string" ? { value: item, label: item } : item;
}

export function itemOf(item: string | PluginItem): PluginItem {
  return typeof item === "string" ? { value: item, label: item } : item;
}

export function isSecretField(field: PluginField): boolean {
  return field.type === "secret" || field.secret === true;
}

/**
 * Whether a field applies given the other fields' values (`show_if`). Pass the
 * values with defaults applied (`withDefaults`): a condition reads what a field shows.
 */
export function isVisible(field: PluginField, values: PluginValues): boolean {
  const condition = field.show_if;
  if (!condition) {
    return true;
  }
  const current = values[condition.key];
  if (condition.equals !== undefined) {
    return current === condition.equals;
  }
  if (condition.in) {
    return condition.in.includes(current as PluginValue);
  }
  return true;
}

/** The values with every unset field's default filled in, for display and conditions. */
export function withDefaults(
  fields: PluginField[],
  values: PluginValues,
): PluginValues {
  const out: PluginValues = {};
  for (const field of fields) {
    if (field.default !== undefined) {
      out[field.key] = field.default;
    }
  }
  return { ...out, ...values };
}

/**
 * The values after picking `choice` on `field`: the field takes it, and the
 * choice's preset fills the fields it names. They stay editable afterwards.
 */
export function applyChoice(
  field: PluginField,
  choice: string,
  values: PluginValues,
): PluginValues {
  const picked = (field.choices ?? [])
    .map(choiceOf)
    .find((item) => item.value === choice);
  return { ...values, ...(picked?.sets ?? {}), [field.key]: choice };
}

/** Fields grouped in the order the manifest lists them; ungrouped fields come first. */
export function groupFields(
  fields: PluginField[],
): Array<{ group: string; fields: PluginField[] }> {
  const groups: Array<{ group: string; fields: PluginField[] }> = [];
  for (const field of fields) {
    const name = field.group ?? "";
    let bucket = groups.find((item) => item.group === name);
    if (!bucket) {
      bucket = { group: name, fields: [] };
      groups.push(bucket);
    }
    bucket.fields.push(field);
  }
  return groups.sort((a, b) => Number(a.group !== "") - Number(b.group !== ""));
}

/** The values a form starts from: what is stored, nothing for a secret. */
export function initialValues(
  fields: PluginField[],
  stored: PluginValues,
): PluginValues {
  const out: PluginValues = {};
  for (const field of fields) {
    if (!isSecretField(field) && stored[field.key] !== undefined) {
      out[field.key] = stored[field.key];
    }
  }
  return out;
}
