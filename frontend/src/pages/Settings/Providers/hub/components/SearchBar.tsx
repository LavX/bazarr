import { FunctionComponent } from "react";
import { CloseButton, TextInput } from "@mantine/core";
import { faMagnifyingGlass } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import styles from "@/pages/Settings/Providers/hub/hub.module.scss";

interface SearchBarProps {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  ariaLabel?: string;
}

export const SearchBar: FunctionComponent<SearchBarProps> = ({
  value,
  onChange,
  placeholder = "Search",
  ariaLabel = "Search",
}) => (
  <TextInput
    className={styles.searchBar}
    value={value}
    onChange={(e) => onChange(e.currentTarget.value)}
    placeholder={placeholder}
    aria-label={ariaLabel}
    leftSection={<FontAwesomeIcon icon={faMagnifyingGlass} />}
    rightSection={
      value ? (
        <CloseButton
          aria-label="Clear search"
          onClick={() => onChange("")}
          size="sm"
        />
      ) : null
    }
  />
);
