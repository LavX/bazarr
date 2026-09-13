import { NavLink, useLocation } from "react-router";
import { Badge, Menu, Tooltip } from "@mantine/core";
import {
  faChevronRight,
  faCircle,
  faFilm,
  faPlay,
  faTrophy,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import logoSrc from "@/assets/images/logo_no_orb128.png";
import { useNavbar } from "@/contexts/Navbar";
import { CustomRouteObject } from "@/Router/type";
import { pathJoin } from "@/utilities";
import AppControls from "./AppControls";
import JobsButton from "./JobsButton";
import styles from "./AppShell.module.scss";

const labels: Record<string, string> = {
  series: "Series",
  movies: "Movies",
  sports: "Sports",
  wanted: "Wanted",
  blacklist: "Excluded",
  "distribution-hub": "Distribution Hub",
};
function visible(route: CustomRouteObject) {
  return (
    !route.hidden &&
    Boolean(route.name && route.path && !route.path.includes(":"))
  );
}

export default function NavigationRail({
  groups,
  routes,
  onOpenJobs,
}: {
  groups: { label: string; items: CustomRouteObject[] }[];
  routes: CustomRouteObject[];
  onOpenJobs: () => void;
}) {
  const { show } = useNavbar();
  const { pathname } = useLocation();
  const connections = routes
    .find((r) => r.path === "settings")
    ?.children?.find(
      (r: CustomRouteObject) => r.path === "connections" && !r.hidden,
    );
  function item(route: CustomRouteObject, setup = false) {
    const path = setup ? "/settings/connections" : pathJoin("/", route.path!);
    const label = labels[route.path!] ?? route.name!;
    const icon = route.icon ?? faCircle;
    const badge =
      typeof route.badge === "string"
        ? route.badge
        : (route.badge ?? 0) +
          (route.children ?? []).reduce(
            (sum, child: CustomRouteObject) =>
              sum +
              (!child.hidden && typeof child.badge === "number"
                ? child.badge
                : 0),
            0,
          );
    const connectionStatus = badge === "LIVE" || badge === "DOWN";
    const iconWithBadge = (
      <span className={styles.railIcon}>
        <FontAwesomeIcon icon={icon} />
        {Boolean(badge) && (
          <span
            className={
              connectionStatus ? styles.connectionDot : styles.railBadge
            }
            data-status={badge}
            aria-hidden="true"
          >
            {connectionStatus
              ? ""
              : typeof badge === "number" && badge > 99
                ? "99+"
                : badge}
          </span>
        )}
      </span>
    );
    const description = badge
      ? connectionStatus
        ? `Connection ${badge.toLowerCase()}`
        : (route.children ?? [])
            .filter(
              (child: CustomRouteObject) =>
                !child.hidden &&
                typeof child.badge === "number" &&
                child.badge > 0,
            )
            .map((child: CustomRouteObject) => `${child.name}: ${child.badge}`)
            .join(", ") || `${badge} items`
      : undefined;
    const children: CustomRouteObject[] = route.children?.filter(visible) ?? [];
    const isCurrent =
      !setup && (pathname === path || pathname.startsWith(path + "/"));
    if (children.length && !route.element) {
      return (
        <Menu
          key={route.path}
          position="right-start"
          width={240}
          withinPortal
          classNames={{ dropdown: styles.navigationMenu }}
        >
          <Menu.Target>
            <button
              type="button"
              className={styles.railLink}
              aria-label={label}
              aria-description={description}
              title={description ? `${label}: ${description}` : label}
              aria-current={isCurrent ? "page" : undefined}
            >
              {iconWithBadge}
              <span>{label}</span>
              <FontAwesomeIcon
                icon={faChevronRight}
                className={styles.menuChevron}
              />
            </button>
          </Menu.Target>
          <Menu.Dropdown>
            <Menu.Label>{route.name}</Menu.Label>
            {children.map((child) => (
              <Menu.Item
                key={child.path}
                component={NavLink}
                to={pathJoin(path, child.path!)}
                onClick={() => show(false)}
                rightSection={
                  child.badge ? (
                    <Badge
                      size="sm"
                      variant="light"
                      aria-label={`${child.name}: ${child.badge}`}
                    >
                      {child.badge}
                    </Badge>
                  ) : undefined
                }
              >
                {child.name}
              </Menu.Item>
            ))}
          </Menu.Dropdown>
        </Menu>
      );
    }
    return (
      <Tooltip
        key={route.path}
        label={
          setup ? `Set up ${label.toLowerCase()} in Connections` : route.name
        }
        position="right"
        openDelay={500}
      >
        <NavLink
          to={path}
          aria-label={setup ? `${label}, set up a library connection` : label}
          aria-description={description}
          aria-current={setup ? "false" : undefined}
          className={styles.railLink}
          onClick={() => show(false)}
        >
          {iconWithBadge}
          <span>{label}</span>
        </NavLink>
      </Tooltip>
    );
  }
  return (
    <div className={styles.railContent}>
      <NavLink
        to="/discover"
        aria-label="Bazarr+ home"
        className={styles.logo}
        onClick={() => show(false)}
      >
        <img src={logoSrc} alt="" width={36} height={36} />
      </NavLink>
      <div className={styles.railScroll}>
        {groups.map((group) => (
          <div
            key={group.label}
            role="group"
            aria-label={group.label}
            className={styles.railGroup}
          >
            {group.items.map((route) => item(route))}
            {group.label === "Media" &&
              connections &&
              ["series", "movies", "sports"]
                .filter(
                  (path) => !group.items.some((route) => route.path === path),
                )
                .map((path) =>
                  item(
                    {
                      path,
                      name: labels[path],
                      icon:
                        path === "series"
                          ? faPlay
                          : path === "sports"
                            ? faTrophy
                            : faFilm,
                    },
                    true,
                  ),
                )}
          </div>
        ))}
      </div>
      <div className={styles.railBottom}>
        <JobsButton onClick={onOpenJobs} />
        <AppControls />
      </div>
    </div>
  );
}
