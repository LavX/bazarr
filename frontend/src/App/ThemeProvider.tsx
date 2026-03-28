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
      '#C1C2D3', '#A9AABB', '#8E8FA3', '#6C6D85', '#53546C',
      '#3D3E55', '#2A2A45', '#22223A', '#1A1A2E', '#121125',
    ],
    // Atmospheric overrides for all Mantine default colors
    gray: [
      '#f4f4f6', '#e8e8ec', '#d0d0d8', '#b0b0be', '#9090a2',
      '#6c6d85', '#53546c', '#3d3e55', '#2a2a45', '#1a1a2e',
    ],
    red: [
      '#fff0f0', '#ffd9d9', '#ffb3b3', '#ff8a8a', '#ff6b6b',
      '#f55050', '#e03e3e', '#c92a2a', '#ad1f1f', '#8b1515',
    ],
    pink: [
      '#fff0f5', '#ffd6e7', '#faa2c1', '#f06595', '#e64980',
      '#d6336c', '#c2255c', '#a61e4d', '#8c1941', '#6e1233',
    ],
    grape: [
      '#f8f0fc', '#ebd6f5', '#d0a2e0', '#b56cc8', '#a050b5',
      '#8b3da0', '#7b2d8e', '#6a1f7a', '#581668', '#460d55',
    ],
    violet: [
      '#f3f0ff', '#e0d9f7', '#bfb3e6', '#9b8ad4', '#7c6abf',
      '#6a52b0', '#5c3fa0', '#4c2f8a', '#3d2070', '#2e1158',
    ],
    indigo: [
      '#edf2ff', '#d5dff7', '#a8bce6', '#7b96d4', '#5c7cbf',
      '#4c6ab0', '#3d5a9e', '#2f4a8a', '#233a70', '#182a58',
    ],
    blue: [
      '#e8f0ff', '#ccddf7', '#99bae6', '#6694d4', '#4078bf',
      '#3568b0', '#2a589e', '#20488a', '#183870', '#102858',
    ],
    cyan: [
      '#f0f5f0', '#d8e8d8', '#b0ccb0', '#88b088', '#6a9a6a',
      '#558855', '#447744', '#336633', '#255525', '#184418',
    ],
    teal: [
      '#f0f5f2', '#d8e8dc', '#b0ccb8', '#88b098', '#6a9a7a',
      '#558868', '#447758', '#336648', '#25553a', '#18442c',
    ],
    green: [
      '#edf7ed', '#d3ebd3', '#a8d8a8', '#7cc47c', '#5cb85c',
      '#4caa4c', '#3d993d', '#2d882d', '#1f751f', '#146014',
    ],
    lime: [
      '#f4fce3', '#e2f5b5', '#c0e078', '#a0c850', '#88b830',
      '#74a822', '#619818', '#4e8510', '#3d700a', '#2d5c06',
    ],
    yellow: [
      '#fff9db', '#fff3bf', '#ffec99', '#ffe066', '#ffd43b',
      '#fcc419', '#fab005', '#f59f00', '#f08c00', '#e67700',
    ],
    orange: [
      '#fff4e6', '#ffe8cc', '#ffd8a8', '#ffc078', '#ffa94d',
      '#ff922b', '#fd7e14', '#e8590c', '#d54a08', '#bf3d04',
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
