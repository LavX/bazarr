/* eslint-disable camelcase -- API field names. */
import { act } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { expect, it, vi } from "vitest";
import { mediaSourcesKey } from "@/apis/hooks/mediaSources";
import { useSportsAvailability } from "@/apis/hooks/sports";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import type { SourceAvailability } from "@/apis/raw/mediaSources";
import { useRoutes } from "@/Router";
import type { CustomRouteObject } from "@/Router/type";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

function Navigation() {
  // eslint-disable-next-line testing-library/no-node-access -- route metadata, not a DOM node.
  const routes: CustomRouteObject[] = useRoutes()[0].children ?? [];
  const sports = useSportsAvailability();
  return (
    <>
      <nav>
        {routes
          .filter((route) => route.name && !route.hidden)
          .map((route) => (
            <div key={route.path}>
              <span>{route.name}</span>
              {((route.children ?? []) as CustomRouteObject[])
                .filter((child) => child.name && !child.hidden)
                .map((child) => (
                  <span
                    key={child.path}
                  >{`${route.name} / ${child.name}`}</span>
                ))}
            </div>
          ))}
      </nav>
      <output>
        {`Sports ${sports.enabled ? "browsable" : "hidden"}, ${sports.searchable ? "searchable" : "search disabled"}`}
      </output>
    </>
  );
}

const ABSENT = { browsable: false, searchable: false, retained: false };
const ENABLED = { ...ABSENT, browsable: true, searchable: true };
const DISABLED = { ...ABSENT, browsable: true };
const RETAINED = { ...ABSENT, retained: true };

it.each([
  ["series", "sonarr", "My series", "Episodes"],
  ["movie", "radarr", "My movies", "Movies"],
  ["sports", "sportarr", "Sports", "Sports"],
] as const)(
  "shows %s library and activity independently of %s",
  async (kind, arr, title, activity) => {
    let visible = true;
    let arrOn = false;
    const availability: SourceAvailability = {
      series: ABSENT,
      movie: ABSENT,
      sports: ABSENT,
    };
    const list = vi.fn();
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({
          general: { [`use_${arr}`]: arrOn },
          media_sources: { visible },
        }),
      ),
      http.get("/api/system/arr-instances", () => HttpResponse.json([])),
      http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
      http.get("/api/system/media-sources", () => {
        list();
        return HttpResponse.json({ data: [], availability });
      }),
      http.get("/api/badges", () => HttpResponse.json({})),
    );
    customRender(<Navigation />);
    const refresh = async (
      update: SourceAvailability[typeof kind],
      useArr = arrOn,
      show = visible,
    ) => {
      availability[kind] = update;
      arrOn = useArr;
      visible = show;
      await act(async () => {
        await queryClient.invalidateQueries({
          queryKey: [QueryKeys.System, QueryKeys.Settings],
        });
        await queryClient.invalidateQueries({
          queryKey: mediaSourcesKey,
          exact: true,
        });
      });
    };
    await waitFor(() => expect(list).toHaveBeenCalled());
    expect(screen.queryByText(title, { exact: true })).not.toBeInTheDocument();
    expect(screen.queryByText(`Missing / ${activity}`)).not.toBeInTheDocument();
    expect(
      screen.queryByText(`Excluded / ${activity}`),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(`History / ${activity}`)).not.toBeInTheDocument();

    await refresh(ABSENT, true);
    expect(await screen.findByText(title, { exact: true })).toBeInTheDocument();
    expect(screen.getByText(`Missing / ${activity}`)).toBeInTheDocument();
    expect(screen.getByText(`Excluded / ${activity}`)).toBeInTheDocument();
    expect(screen.getByText(`History / ${activity}`)).toBeInTheDocument();

    await refresh(ENABLED, false);
    if (kind === "sports") {
      expect(
        await screen.findByText("Sports browsable, searchable"),
      ).toBeInTheDocument();
      expect(screen.getByText(`History / ${activity}`)).toBeInTheDocument();
    } else {
      await waitFor(() =>
        expect(
          screen.queryByText(`History / ${activity}`),
        ).not.toBeInTheDocument(),
      );
    }
    expect(await screen.findByText(title, { exact: true })).toBeInTheDocument();
    expect(screen.getByText(`Missing / ${activity}`)).toBeInTheDocument();
    expect(screen.getByText(`Excluded / ${activity}`)).toBeInTheDocument();

    await refresh(DISABLED);
    expect(screen.getByText(title, { exact: true })).toBeInTheDocument();
    expect(screen.getByText(`Missing / ${activity}`)).toBeInTheDocument();
    if (kind === "sports")
      expect(
        await screen.findByText("Sports browsable, search disabled"),
      ).toBeInTheDocument();

    await refresh(RETAINED);
    await waitFor(() =>
      expect(
        screen.queryByText(title, { exact: true }),
      ).not.toBeInTheDocument(),
    );
    expect(screen.getByText(`Missing / ${activity}`)).toBeInTheDocument();
    expect(screen.getByText(`Excluded / ${activity}`)).toBeInTheDocument();
    if (kind === "sports")
      expect(screen.getByText(`History / ${activity}`)).toBeInTheDocument();

    await refresh(ENABLED, false, false);
    await waitFor(() =>
      expect(
        screen.queryByText(`Missing / ${activity}`),
      ).not.toBeInTheDocument(),
    );
    expect(screen.queryByText(title, { exact: true })).not.toBeInTheDocument();
    expect(
      screen.queryByText(`Excluded / ${activity}`),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(`History / ${activity}`)).not.toBeInTheDocument();

    await refresh(RETAINED, true, false);
    expect(await screen.findByText(title, { exact: true })).toBeInTheDocument();
    expect(screen.getByText(`Missing / ${activity}`)).toBeInTheDocument();
    expect(screen.getByText(`Excluded / ${activity}`)).toBeInTheDocument();
    expect(screen.getByText(`History / ${activity}`)).toBeInTheDocument();
  },
);
