/**
 * Flags a root-relative URL where Playwright resolves it against the host
 * instead of the path the instance is served under. `page.goto`, the API
 * request helpers, `expect(...).toHaveURL` and `page.waitForURL` all resolve
 * `"/path"` against the baseURL's host, dropping any path in the baseURL,
 * and a leading-slash path replaces the whole path of any base it is given:
 * `new URL("/path", "http://host/bazarr")` is `http://host/path`. Against an
 * instance with `general.base_url` set, such as `/bazarr`, each of these
 * reaches the host's root, and a spec can then pass against the wrong page.
 *
 * Build every page and API path with `appPath()` from `e2e/lib/base.ts`,
 * which resolves it against the base. The rule is an early warning while a
 * spec is written: a run under a sub-path (see docs/agents/e2e.md) is what
 * guards the forms it cannot see, such as a path held in a variable.
 */
import type { Rule } from "eslint";
import type * as ESTree from "estree";

/** Member calls that take the URL as their first argument. */
const URL_ARGUMENTS = new Set([
  "goto",
  "get",
  "post",
  "put",
  "patch",
  "delete",
  "head",
  "fetch",
  "toHaveURL",
  "waitForURL",
]);

type Argument = ESTree.Expression | ESTree.SpreadElement;

/** The literal itself when `node` is a string or template that starts with a slash. */
function rootRelativeLiteral(
  node: Argument | undefined,
): ESTree.Literal | ESTree.TemplateLiteral | null {
  if (node === undefined) return null;
  if (
    node.type === "Literal" &&
    typeof node.value === "string" &&
    node.value.startsWith("/")
  ) {
    return node;
  }
  if (
    node.type === "TemplateLiteral" &&
    (node.quasis[0]?.value.raw ?? "").startsWith("/")
  ) {
    return node;
  }
  return null;
}

function check(
  context: Rule.RuleContext,
  node: ESTree.CallExpression | ESTree.NewExpression,
): void {
  const url = rootRelativeLiteral(node.arguments[0]);
  if (url === null) return;
  context.report({ node: url, messageId: "rootRelative" });
}

export const noRootRelativeUrl: Rule.RuleModule = {
  meta: {
    type: "problem",
    docs: {
      description:
        "Disallows a root-relative URL where Playwright resolves it against the host, not the instance's base path",
    },
    schema: [],
    messages: {
      rootRelative:
        "This URL is root-relative, so Playwright resolves it against the host, not against the path the instance is served under. Build it with appPath() from e2e/lib/base.",
    },
  },
  create(context) {
    return {
      CallExpression(node: ESTree.CallExpression & Rule.NodeParentExtension) {
        const callee = node.callee;
        if (
          callee.type !== "MemberExpression" ||
          callee.computed ||
          callee.property.type !== "Identifier" ||
          !URL_ARGUMENTS.has(callee.property.name)
        ) {
          return;
        }
        check(context, node);
      },
      NewExpression(node: ESTree.NewExpression & Rule.NodeParentExtension) {
        // However many arguments it has: a leading-slash path is the whole
        // path of the result, so new URL("/path", base) drops the base's.
        if (node.callee.type !== "Identifier" || node.callee.name !== "URL") {
          return;
        }
        check(context, node);
      },
    };
  },
};

export default noRootRelativeUrl;
