import { FunctionComponent, ReactNode, useId } from "react";
import { Input } from "@mantine/core";

export interface GroupWrapperProps {
  label: ReactNode;
  description?: ReactNode;
  error?: ReactNode;
  role?: "group" | "radiogroup" | "list";
  children: ReactNode;
}

/**
 * Label, help and error for a control made of several parts (a segmented
 * choice, a key list, a sortable list). The parts sit in a group that is named
 * by the label and described by the help and the error, the way a single input
 * is, and the error is announced when it appears.
 */
const GroupWrapper: FunctionComponent<GroupWrapperProps> = ({
  label,
  description,
  error,
  role = "group",
  children,
}) => {
  const id = useId();
  const describedBy =
    [error ? `${id}-error` : "", description ? `${id}-description` : ""]
      .filter(Boolean)
      .join(" ") || undefined;
  return (
    <Input.Wrapper
      id={id}
      labelElement="div"
      label={label}
      description={description}
      error={error}
      errorProps={{ role: "alert" }}
    >
      <div
        role={role}
        aria-labelledby={`${id}-label`}
        aria-describedby={describedBy}
        style={{ display: "grid", gap: 6 }}
      >
        {children}
      </div>
    </Input.Wrapper>
  );
};

export default GroupWrapper;
