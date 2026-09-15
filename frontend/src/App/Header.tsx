import { FunctionComponent, useState } from "react";
import { useLocation } from "react-router";
import { AppShell, Burger, Text } from "@mantine/core";
import { useWindowEvent } from "@mantine/hooks";
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

/**
 * Past this the header stops being transparent.
 *
 * A few pixels rather than zero, so a rubber-band scroll or a one-pixel jitter
 * does not flicker the background on and off.
 */
const SCROLLED_PAST = 4;

const AppHeader: FunctionComponent = () => {
  const { show, showed } = useNavbar();
  // At the top the header has nothing behind it but the page's own background,
  // so a surface there only cuts the page in two: the hero glow reaches up into
  // this band and an opaque strip sliced straight through it. Once content
  // starts passing underneath it needs a surface again, and a translucent one
  // keeps the page visible without letting it fight the title and the search.
  const [scrolled, setScrolled] = useState(
    () => typeof window !== "undefined" && window.scrollY > SCROLLED_PAST,
  );
  useWindowEvent("scroll", () => setScrolled(window.scrollY > SCROLLED_PAST));
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
    <AppShell.Header className={styles.appHeader} data-scrolled={scrolled}>
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
