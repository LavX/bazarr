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
// Equal the way the backend compares for a condition: lists by their members in order, not by identity.
function sameValue(a: unknown, b: unknown): boolean {
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((item, i) => sameValue(item, b[i]));
  }
  return a === b;
}

export function isVisible(field: PluginField, values: PluginValues): boolean {
  const condition = field.show_if;
  if (!condition) {
    return true;
  }
  const current = values[condition.key];
  if (condition.equals !== undefined) {
    return sameValue(current, condition.equals);
  }
  if (condition.in) {
    return condition.in.some((option) => sameValue(current, option));
  }
  return true;
}

// What the API treats as "not set": it drops these on save, so the field's default applies again. A toggle
// is the one type that keeps them, because off is a value.
const isUnset = (value: PluginValue | undefined) =>
  value === null ||
  value === undefined ||
  value === "" ||
  (Array.isArray(value) && value.length === 0);

/**
 * The values with every unset field's default filled in, for display and conditions. It applies the
 * rule the API applies on save: null, an empty string and an empty list are no value, so the default
 * stands (and a toggle keeps its value, whatever it is). A condition therefore sees what the saved
 * form will hold. The form's own values are not changed: they keep the empty entry, which is what
 * resets the field on save.
 */
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
  const toggles = new Set(
    fields.filter((field) => field.type === "toggle").map((f) => f.key),
  );
  for (const [key, value] of Object.entries(values)) {
    if (toggles.has(key) ? value !== undefined : !isUnset(value)) {
      out[key] = value;
    }
  }
  return out;
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

/**
 * The values a form starts from: what is stored, nothing for a secret. A list the user can reorder that has
 * no stored value starts as the order it is shown in (its default, or the manifest's item order), so what
 * the form displays is what it holds and what a save sends.
 */
export function initialValues(
  fields: PluginField[],
  stored: PluginValues,
): PluginValues {
  const out: PluginValues = {};
  for (const field of fields) {
    if (isSecretField(field)) {
      continue;
    }
    if (stored[field.key] !== undefined) {
      out[field.key] = stored[field.key];
    } else if (field.type === "orderable_list") {
      const shown = Array.isArray(field.default) ? field.default : [];
      out[field.key] = shown.length
        ? shown
        : (field.items ?? []).map((item) => itemOf(item).value);
    }
  }
  return out;
}
