import { FunctionComponent, PropsWithChildren } from "react";
import NotFound from "@/pages/errors/NotFound";
import SystemStatusGate from "./SystemStatusGate";

/**
 * The Translation page belongs to the v2 translation runtime. While the
 * runtime is off its address is Not Found, exactly like an address that never
 * existed, and nothing of the page is mounted or fetched.
 *
 * The gate is inside the route instead of around it on purpose. A route that
 * is added to the list only once the status has answered shifts the position
 * of every route after it, and the router keeps the match it made before the
 * change by position: a page opened by its address then draws a neighbouring
 * route until the next navigation. A route that is always there keeps every
 * position where it was.
 */
const TranslationGate: FunctionComponent<PropsWithChildren> = ({
  children,
}) => (
  <SystemStatusGate>
    {(runtime) => (runtime === "ready" ? children : <NotFound />)}
  </SystemStatusGate>
);

export default TranslationGate;
