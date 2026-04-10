import { RouteObject } from "react-router";
import { IconDefinition } from "@fortawesome/free-solid-svg-icons";

declare namespace Route {
  export type Item = {
    icon?: IconDefinition;
    name?: string;
    badge?: number | string;
    hidden?: boolean;
    children?: Item[];
    /** Render a labeled divider above this item in the nav */
    divider?: string;
  };
}

export type CustomRouteObject = RouteObject & Route.Item;
