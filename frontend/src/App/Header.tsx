import { FunctionComponent } from "react";
import { useLocation } from "react-router";
import { AppShell, Burger, Text } from "@mantine/core";
import UniversalSearch from "@/components/UniversalSearch";
import { useNavbar } from "@/contexts/Navbar";
import { useRouteItems } from "@/Router";
import { CustomRouteObject } from "@/Router/type";
import styles from "./AppShell.module.scss";

const pageNames: Record<string, string> = {
  discover: "Discover",
  series: "Series",
  movies: "Movies",
  sports: "Sports",
  wanted: "Wanted",
  history: "History",
  blacklist: "Excluded subtitles",
  "subtitle-hub": "Subtitle Hub",
  "distribution-hub": "Distribution Hub",
  settings: "Settings",
  system: "System",
  subtitles: "Subtitle editor",
};

const AppHeader: FunctionComponent = () => {
  const { show, showed } = useNavbar();
  const { pathname } = useLocation();
  const parts = pathname.split("/");
  const routes = useRouteItems();
  const section = pageNames[parts[1]] ?? "Bazarr+";
  const appRoutes = routes.find((route) => route.path === "/")?.children ?? [];
  const parent = appRoutes.find((route) => route.path === parts[1]);
  const subpage = parent?.children?.find(
    (route: CustomRouteObject) => !route.hidden && route.path === parts[2],
  ) as CustomRouteObject | undefined;
  const page =
    ["system", "settings"].includes(parts[1]) && subpage?.name
      ? subpage.name
      : section;
  return (
    <AppShell.Header className={styles.appHeader}>
      <div className={styles.headerLayout}>
        <div className={styles.headerIdentity}>
          <Burger
            aria-label={showed ? "Close navigation" : "Open navigation"}
            aria-expanded={showed}
            opened={showed}
            onClick={() => show(!showed)}
            size="sm"
            hiddenFrom="sm"
          />
          <Text
            className={styles.pageName}
            title={page === section ? page : `${section} / ${page}`}
          >
            {page}
          </Text>
        </div>
        <div className={styles.headerSearch}>
          <UniversalSearch />
        </div>
      </div>
    </AppShell.Header>
  );
};
export default AppHeader;
