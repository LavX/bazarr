import { FunctionComponent, PropsWithChildren } from "react";
import {
  ActionIcon,
  Badge,
  Button,
  createTheme,
  Input,
  MantineProvider,
  Pagination,
  Progress,
} from "@mantine/core";
import ThemeLoader from "@/App/ThemeLoader";
import "@mantine/core/styles.layer.css";
import "@mantine/notifications/styles.layer.css";
import styleVars from "@/assets/_variables.module.scss";
import actionIconClasses from "@/assets/action_icon.module.scss";
import badgeClasses from "@/assets/badge.module.scss";
import buttonClasses from "@/assets/button.module.scss";
import paginationClasses from "@/assets/pagination.module.scss";
import inputClasses from "@/assets/input.module.scss";
import progressClasses from "@/assets/progress.module.scss";

const themeProvider = createTheme({
  fontFamily: "'Geist Variable', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
  colors: {
    brand: [
      styleVars.colorBrand0,
      styleVars.colorBrand1,
      styleVars.colorBrand2,
      styleVars.colorBrand3,
      styleVars.colorBrand4,
      styleVars.colorBrand5,
      styleVars.colorBrand6,
      styleVars.colorBrand7,
      styleVars.colorBrand8,
      styleVars.colorBrand9,
    ],
    dark: [
      '#C1C2D3',  // dark.0
      '#A9AABB',  // dark.1
      '#8E8FA3',  // dark.2
      '#6C6D85',  // dark.3
      '#53546C',  // dark.4
      '#3D3E55',  // dark.5
      '#2A2A45',  // dark.6
      '#22223A',  // dark.7
      '#1A1A2E',  // dark.8
      '#121125',  // dark.9
    ],
  },
  primaryColor: "brand",
  components: {
    ActionIcon: ActionIcon.extend({
      classNames: actionIconClasses,
    }),
    Badge: Badge.extend({
      classNames: badgeClasses,
    }),
    Button: Button.extend({
      classNames: buttonClasses,
    }),
    Input: Input.extend({
      classNames: inputClasses,
    }),
    Pagination: Pagination.extend({
      classNames: paginationClasses,
    }),
    Progress: Progress.extend({
      classNames: progressClasses,
    }),
  },
});

const ThemeProvider: FunctionComponent<PropsWithChildren> = ({ children }) => {
  return (
    <MantineProvider theme={themeProvider} defaultColorScheme="auto">
      <ThemeLoader />
      {children}
    </MantineProvider>
  );
};

export default ThemeProvider;
