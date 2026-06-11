import {
  ComponentProps,
  FunctionComponent,
  JSX,
  PropsWithChildren,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { Button, ButtonProps, Text } from "@mantine/core";
import { IconDefinition } from "@fortawesome/fontawesome-svg-core";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { LOG } from "@/utilities/console";

type ToolboxButtonProps = Omit<ButtonProps, "color" | "variant" | "leftIcon"> &
  Omit<ComponentProps<"button">, "ref"> & {
    icon: IconDefinition;
    children: string;
  };

const ToolboxButton: FunctionComponent<ToolboxButtonProps> = ({
  icon,
  children,
  ...props
}) => {
  return (
    <Button
      color="dark"
      variant="subtle"
      leftSection={<FontAwesomeIcon icon={icon}></FontAwesomeIcon>}
      {...props}
    >
      <Text size="xs">{children}</Text>
    </Button>
  );
};

type ToolboxMutateButtonProps<R, T extends () => Promise<R>> = {
  promise: T;
  onSuccess?: (item: R) => void;
} & Omit<ToolboxButtonProps, "onClick" | "loading">;

export function ToolboxMutateButton<R, T extends () => Promise<R>>(
  props: PropsWithChildren<ToolboxMutateButtonProps<R, T>>,
): JSX.Element {
  const { promise, onSuccess, ...button } = props;

  const [loading, setLoading] = useState(false);

  // The toolbar hosting this button can unmount mid-request (e.g. when the
  // dirty selection clears after a save), so guard against setState-after-unmount.
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const click = useCallback(async () => {
    setLoading(true);
    try {
      const val = await promise();
      if (mountedRef.current) {
        onSuccess && onSuccess(val);
      }
    } catch (error) {
      // The user-facing notification is already surfaced by the axios response
      // interceptor (handleError). Just log here so the rejection is not
      // silently swallowed, and let finally reset the spinner.
      LOG("error", "Toolbox mutation failed", error);
    } finally {
      if (mountedRef.current) {
        setLoading(false);
      }
    }
  }, [onSuccess, promise]);

  return (
    <ToolboxButton
      loading={loading}
      onClick={click}
      {...button}
    ></ToolboxButton>
  );
}

export default ToolboxButton;
