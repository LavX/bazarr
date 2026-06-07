import React, {
  createContext,
  FunctionComponent,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { matchPath, NavLink, RouteObject, useLocation } from "react-router";
import {
  ActionIcon,
  AppShell,
  Badge,
  Collapse,
  Stack,
  Text,
  Tooltip,
} from "@mantine/core";
import { useHover } from "@mantine/hooks";
import { faGift, IconDefinition } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import clsx from "clsx";
import { useSystemStatus } from "@/apis/hooks";
import {
  hasWhatsNew,
  useOpenWhatsNew,
} from "@/components/modals/useWhatsNewAutoOpen";
import { useNavbar } from "@/contexts/Navbar";
import { useRouteItems } from "@/Router";
import { CustomRouteObject, Route } from "@/Router/type";
import { BuildKey, pathJoin } from "@/utilities";
import { LOG } from "@/utilities/console";
import styles from "./Navbar.module.scss";

const Selection = createContext<{
  selection: string | null;
  select: (path: string | null) => void;
}>({
  selection: null,
  select: () => {
    LOG("error", "Selection context not initialized");
  },
});

function useSelection() {
  return useContext(Selection);
}

function useBadgeValue(route: Route.Item) {
  const { badge, children } = route;
  return useMemo(() => {
    if (typeof badge === "string") {
      return badge;
    }

    let value = badge ?? 0;

    if (children === undefined) {
      return value;
    }

    value +=
      children.reduce((acc, child: Route.Item) => {
        const childBadgeValue = child.badge;
        if (typeof childBadgeValue === "number" && child.hidden !== true) {
          return acc + childBadgeValue;
        }
        return acc;
      }, 0) ?? 0;

    return value === 0 ? undefined : value;
  }, [badge, children]);
}

function useIsActive(parent: string, route: RouteObject) {
  const { path, children } = route;

  const { pathname } = useLocation();
  const root = useMemo(() => pathJoin(parent, path ?? ""), [parent, path]);

  const paths = useMemo(
    () => [root, ...(children?.map((v) => pathJoin(root, v.path ?? "")) ?? [])],
    [root, children],
  );

  const selection = useSelection().selection;
  return useMemo(
    () =>
      selection?.includes(root) ||
      paths.some((path) => matchPath(path, pathname)),
    [pathname, paths, root, selection],
  );
}

// Section grouping configuration.
// Routes are matched by their path property.
const sectionGroups = [
  { label: "Media", paths: ["series", "movies"] },
  { label: "Management", paths: ["history", "wanted", "blacklist"] },
  {
    label: "System",
    paths: ["subtitle-hub", "distribution-hub", "settings", "system"],
  },
];

function groupRoutes(routes: CustomRouteObject[]) {
  // Filter to visible nav items (have a path, not hidden, not index-only)
  const navItems = routes.filter(
    (r) => r.path !== undefined && !r.hidden && !r.path.includes(":") && r.name,
  );

  const groups: { label: string; items: CustomRouteObject[] }[] = [];

  for (const section of sectionGroups) {
    const items = section.paths
      .map((p) => navItems.find((r) => r.path === p))
      .filter((r): r is CustomRouteObject => r !== undefined);

    if (items.length > 0) {
      groups.push({ label: section.label, items });
    }
  }

  // Catch any remaining items not in a defined group
  const groupedPaths = new Set(sectionGroups.flatMap((s) => s.paths));
  const ungrouped = navItems.filter((r) => !groupedPaths.has(r.path ?? ""));
  if (ungrouped.length > 0) {
    groups.push({ label: "Other", items: ungrouped });
  }

  return groups;
}

const AppNavbar: FunctionComponent = () => {
  const [selection, select] = useState<string | null>(null);

  const routes = useRouteItems();
  const { data: status } = useSystemStatus();
  const openWhatsNew = useOpenWhatsNew();
  const showWhatsNew = hasWhatsNew();

  const { pathname } = useLocation();
  useEffect(() => {
    select(null);
  }, [pathname]);

  // The top-level route (path "/") contains the nav items as children.
  // useRouteItems returns the full routes array, and the nameless "/" route
  // renders its children directly. We need to find the app route's children.
  const navRoutes = useMemo(() => {
    const appRoute = routes.find((r) => r.path === "/");
    return appRoute?.children ?? routes;
  }, [routes]);

  const groups = useMemo(() => groupRoutes(navRoutes), [navRoutes]);

  return (
    <AppShell.Navbar className={styles.nav}>
      <div className={styles.navInner}>
        <Selection.Provider value={{ selection, select }}>
          <Stack gap={0}>
            {groups.map((group) => {
              const groupId = `nav-group-${group.label.toLowerCase().replace(/\s+/g, "-")}`;
              return (
                <div key={group.label} role="group" aria-labelledby={groupId}>
                  <div
                    id={groupId}
                    role="heading"
                    aria-level={2}
                    className={styles.groupLabel}
                  >
                    {group.label}
                  </div>
                  {group.items.map((route, idx) => (
                    <RouteItem
                      key={BuildKey("nav", group.label, idx)}
                      parent="/"
                      route={route}
                    />
                  ))}
                </div>
              );
            })}
          </Stack>
        </Selection.Provider>
      </div>
      <div className={styles.navFooter}>
        <Text size="xs" c="var(--bz-text-tertiary)" truncate>
          {status?.bazarr_version ?? ""}
        </Text>
        {showWhatsNew && (
          <Tooltip label="What's new" position="top" withArrow>
            <ActionIcon
              variant="subtle"
              aria-label="What's new"
              onClick={openWhatsNew}
            >
              <FontAwesomeIcon icon={faGift} />
            </ActionIcon>
          </Tooltip>
        )}
      </div>
    </AppShell.Navbar>
  );
};

const RouteItem: FunctionComponent<{
  route: CustomRouteObject;
  parent: string;
}> = ({ route, parent }) => {
  const { children, name, path, icon, hidden, element, divider } = route;

  const { select } = useSelection();

  const link = useMemo(() => pathJoin(parent, path ?? ""), [parent, path]);

  const badge = useBadgeValue(route);

  const isOpen = useIsActive(parent, route);

  // Ignore path if it is using match
  if (hidden === true || path === undefined || path.includes(":")) {
    return null;
  }

  if (children !== undefined) {
    const elements = (
      <Stack gap={0}>
        {children.map((child, idx) => (
          <RouteItem
            parent={link}
            key={BuildKey(link, "nav", idx)}
            route={child}
          />
        ))}
      </Stack>
    );

    if (name) {
      return (
        <Stack gap={0}>
          <NavbarItem
            name={name}
            link={link}
            icon={icon}
            badge={badge}
            onClick={(event) => {
              LOG("info", "clicked", link);

              const validated =
                element !== undefined ||
                children?.find((v) => v.index === true) !== undefined;

              if (!validated) {
                event.preventDefault();
              }

              if (isOpen) {
                select(null);
              } else {
                select(link);
              }
            }}
          />
          <Collapse hidden={children.length === 0} expanded={isOpen}>
            {elements}
          </Collapse>
        </Stack>
      );
    } else {
      return elements;
    }
  } else {
    return (
      <>
        {divider && <div className={styles.subGroupLabel}>{divider}</div>}
        <NavbarItem name={name ?? link} link={link} icon={icon} badge={badge} />
      </>
    );
  }
};

interface NavbarItemProps {
  name: string;
  link: string;
  icon?: IconDefinition;
  badge?: number | string;
  onClick?: (event: React.MouseEvent<HTMLAnchorElement>) => void;
}

const NavbarItem: FunctionComponent<NavbarItemProps> = ({
  icon,
  link,
  name,
  badge,
  onClick,
}) => {
  const { show } = useNavbar();

  const { ref, hovered } = useHover();

  const shouldHideBadge = useMemo(() => {
    if (typeof badge === "number") {
      return badge === 0;
    } else if (typeof badge === "string") {
      return badge.length === 0;
    }

    return true;
  }, [badge]);

  const isSignalRBadge = useMemo(() => {
    return link === "/series" || link === "/movies";
  }, [link]);

  // Compute explicit background and text style objects safely
  const badgeStyle = useMemo(() => {
    if (!isSignalRBadge) return {};

    if (badge === "LIVE") {
      return {
        // Subtle background colours that adapt to light/dark mode
        backgroundColor:
          "light-dark(var(--mantine-color-gray-0), var(--mantine-color-dark-6))",

        // Softened high-contrast text colors
        color:
          "light-dark(var(--mantine-color-gray-7), var(--mantine-color-gray-5))",

        border: "none",
      };
    }

    if (badge === "DOWN") {
      return {
        // more noticeable background colors for "DOWN" status, still adapting to theme
        backgroundColor:
          "light-dark(var(--mantine-color-red-6), var(--mantine-color-red-8))",
        color: "var(--mantine-color-white)",
      };
    }

    return {};
  }, [badge, isSignalRBadge]);

  return (
    <NavLink
      to={link}
      onClick={(event: React.MouseEvent<HTMLAnchorElement>) => {
        onClick?.(event);
        if (!event.isDefaultPrevented()) {
          show(false);
        }
      }}
      className={({ isActive }) =>
        clsx(
          clsx(styles.anchor, {
            [styles.active]: isActive,
            [styles.hover]: hovered,
          }),
        )
      }
    >
      <Text ref={ref} inline className={styles.text} span>
        {icon && <FontAwesomeIcon className={styles.icon} icon={icon} />}
        {name}
        {!shouldHideBadge && (
          <Badge
            className={styles.badge}
            variant="filled"
            radius="xs"
            style={badgeStyle}
          >
            {badge}
          </Badge>
        )}
      </Text>
    </NavLink>
  );
};
export default AppNavbar;
