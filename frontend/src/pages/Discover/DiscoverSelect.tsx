import { ComboboxItem, Select, SelectProps } from "@mantine/core";
import styles from "./Discover.module.scss";

/**
 * One themed select for every choice on Discover.
 *
 * A native select hands its list to the operating system, which paints an
 * unstyled white listbox over the page. This one renders in the page, in the
 * page's palette, and filters as the reader types once the list is long
 * enough to need it: a region list of 250 entries or a language list of 190
 * is a type-ahead, not a scroll. Keyboard operation is the combobox pattern:
 * arrows open and move, Enter chooses, Escape closes, typing filters.
 *
 * Values are the caller's own strings, with "" for nothing chosen, so callers
 * keep the contract they had with the native control.
 */
export default function DiscoverSelect({
  value,
  onChange,
  options,
  searchable,
  className,
  classNames,
  ...rest
}: Omit<SelectProps, "data" | "value" | "onChange" | "searchable"> & {
  value: string;
  onChange: (value: string) => void;
  options: ComboboxItem[];
  /** Defaults to a type-to-filter list once it holds more than eight rows. */
  searchable?: boolean;
}) {
  const filter = searchable ?? options.length > 8;
  return (
    <Select
      {...rest}
      className={[styles.select, className].filter(Boolean).join(" ")}
      classNames={{
        input: styles.selectInput,
        dropdown: styles.selectDropdown,
        option: styles.selectOption,
        ...classNames,
      }}
      data={options}
      value={value === "" ? null : value}
      onChange={(next) => onChange(next ?? "")}
      searchable={filter}
      selectFirstOptionOnChange={filter}
      nothingFoundMessage={filter ? "No matches" : undefined}
      allowDeselect={false}
      checkIconPosition="right"
      maxDropdownHeight={320}
      comboboxProps={{
        withinPortal: true,
        // A closed list leaves the DOM. Kept mounted, its listbox is labelled
        // by the field's label and answers for it in assistive technology.
        keepMounted: false,
        shadow: "md",
        transitionProps: { duration: 0 },
      }}
    />
  );
}
