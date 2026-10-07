/**
 * Flags the request shortcuts a spec could take around the suite's sub-path
 * guard, and the creation of a context, a page or a browser the guard
 * cannot cover. Playwright hands every page and browser context a request
 * context of its own, and those follow redirects and fire no page request
 * events, so under a sub-path a root-relative URL through them reaches
 * the host's root and a spec can pass against the wrong answer while the
 * context-level guard sees none of it. The same goes for anything created
 * outside the fixtures: the watch and the guarded request property are
 * installed on the context the fixtures hand out, so a fresh context,
 * page or browser has neither. launchPersistentContext and connectOverCDP
 * are matched on the method name, because a browser hands the same
 * methods out under any name a spec renames them to, and the engines
 * themselves are flagged as members and as destructured keys, because a
 * browser reached through chromium, firefox, webkit or browserType is
 * created under any name those forms bind it to. A bracket member with
 * a literal property is read like a named one, because the literal
 * names the same member the dot form would, and the creation methods
 * newContext, newPage and the two launch forms are flagged as
 * destructured keys like the engines are, because a method destructured
 * out of a browser or a factory creates the same unwatched thing under
 * the new name, a newPage destructured off the fixture context being
 * the one clean form, a page of it being watched like any other. The
 * engine imports (request, chromium, firefox and webkit) are flagged
 * themselves,
 * because a renamed import is the one form a name-based match cannot
 * see. At runtime the fixtures refuse a request context the factory is
 * asked for under a base path, so a request context the factory makes
 * that slips past this rule does not run unguarded.
 *
 * Use the suite's `api` fixture instead: it sends the API key on every call
 * and refuses, before sending, a URL that resolves outside the base the
 * instance is served under. Playwright's own `request` fixture is refused
 * under a base path for the same reason. The suite's own context, its
 * pages and the login are what the fixtures and global setup provide.
 */
import type { Rule } from "eslint";
import type * as ESTree from "estree";

/** What the suite's specs call a page or a browser context. */
const HOLDERS = new Set(["page", "context"]);
/** The fixture context: the one context whose pages are watched. */
const FIXTURE_CONTEXT = "context";
/**
 * The creation calls matched on the method name: a browser of the spec's
 * own, however the holder was renamed. A plain launch or connect is not
 * one of them, because a spec's own client can carry them, a database
 * client connecting, say; the engines a browser would come from are what
 * the rule flags instead.
 */
const CREATORS = new Set(["launchPersistentContext", "connectOverCDP"]);
/**
 * The methods that make a context, a page or a browser, matched as
 * destructured keys like the engines are: the key names what the spec
 * then calls, whatever it binds the value to.
 */
const CREATION_METHODS = new Set(["newContext", "newPage", ...CREATORS]);
/**
 * The engine members a spec could reach a browser factory through,
 * flagged as members and as destructured keys: whatever call the spec
 * then makes, a browser made through one of them gets neither the
 * watch nor the guarded request.
 */
const ENGINES = new Set(["chromium", "firefox", "webkit", "browserType"]);
/** What a spec could create an unwatched context, a page or a browser with. */
const IMPORTS = new Set(["request", "chromium", "firefox", "webkit"]);
/** Where those imports come from, the test package and the engines. */
const SOURCES = new Set(["@playwright/test", "playwright", "playwright-core"]);
/** Where the suite itself creates its guarded context, pages and login. */
const OWN_FILES = ["e2e/fixtures/index.ts", "e2e/global-setup.ts"];

/**
 * The static name of a member: the property of a dot member, or the
 * literal string of a bracket one. A member read through brackets with
 * a literal names the same member the dot form would, so the rule reads
 * both alike; a property held in a variable is the one form it cannot
 * see, the same literal the URL rule leaves alone.
 */
function memberName(node: ESTree.MemberExpression): string | null {
  if (!node.computed && node.property.type === "Identifier") {
    return node.property.name;
  }
  if (
    node.computed &&
    node.property.type === "Literal" &&
    typeof node.property.value === "string"
  ) {
    return node.property.value;
  }
  return null;
}

/**
 * Whether `object` is the fixture context itself, the one whose pages are
 * watched: the context under its own name, or page.context(), which is
 * that same context.
 */
function isFixtureContext(object: ESTree.Expression | ESTree.Super): boolean {
  if (object.type === "Identifier") return object.name === FIXTURE_CONTEXT;
  return (
    object.type === "CallExpression" &&
    object.callee.type === "MemberExpression" &&
    !object.callee.computed &&
    object.callee.property.type === "Identifier" &&
    object.callee.property.name === "context" &&
    object.callee.object.type === "Identifier" &&
    object.callee.object.name === "page"
  );
}

export const noRequestShortcut: Rule.RuleModule = {
  meta: {
    type: "problem",
    docs: {
      description:
        "Disallows page.request and context.request, the creation of a context, page or browser outside the fixtures and the engines and creation methods that do it, as imports, members, bracket members and destructured keys, which go around the suite's sub-path guard; use the api fixture and the suite's context",
    },
    schema: [],
    messages: {
      requestShortcut:
        "This request context follows redirects and fires no page request events, so a URL that missed the base passes unseen. Use the api fixture, which refuses such a URL before sending it.",
      unguardedCreation:
        "A context, page or browser created here gets neither the watch nor the guarded request, so under a sub-path a URL that missed the base through it passes unseen. Use the suite's context, its pages and the api fixture.",
      unguardedImport:
        "This import is how a context or a browser of the spec's own is created, renamed however the spec likes: a request context through request, a browser and its contexts through chromium, firefox or webkit. Use the api fixture and the suite's context.",
    },
  },
  create(context) {
    // Where the suite itself creates its guarded context, its pages and the
    // login: the creation and import checks leave those files alone, the
    // request shortcuts are flagged everywhere.
    const own = OWN_FILES.some((name) => context.filename.endsWith(name));
    return {
      MemberExpression(
        node: ESTree.MemberExpression & Rule.NodeParentExtension,
      ) {
        const name = memberName(node);
        if (name === null) return;
        if (
          node.object.type === "Identifier" &&
          HOLDERS.has(node.object.name) &&
          name === "request"
        ) {
          context.report({ node: node.property, messageId: "requestShortcut" });
          return;
        }
        if (own) return;
        // A context of the spec's own, however it was reached: the method
        // is the same one whatever name holds the factory or the browser.
        if (name === "newContext") {
          context.report({
            node: node.property,
            messageId: "unguardedCreation",
          });
          return;
        }
        // A page of the fixture context is watched; one of anything else,
        // another context included, is not.
        if (name === "newPage" && !isFixtureContext(node.object)) {
          context.report({
            node: node.property,
            messageId: "unguardedCreation",
          });
          return;
        }
        // A browser of the spec's own, however it is launched or connected.
        if (CREATORS.has(name)) {
          context.report({
            node: node.property,
            messageId: "unguardedCreation",
          });
          return;
        }
        // The engine members themselves: a browser factory reached through
        // playwright, whatever call the spec then makes on it.
        if (ENGINES.has(name)) {
          context.report({
            node: node.property,
            messageId: "unguardedCreation",
          });
        }
      },
      ObjectPattern(node: ESTree.ObjectPattern & Rule.NodeParentExtension) {
        if (own) return;
        // What the pattern destructures from, where it has a source: the
        // init of a declaration or the right of an assignment. A pattern
        // without one, a parameter, say, is never the fixture context.
        const parent = node.parent;
        const source =
          parent?.type === "VariableDeclarator"
            ? (parent.init ?? null)
            : parent?.type === "AssignmentExpression" && parent.left === node
              ? parent.right
              : null;
        // The engines and the creation methods destructured out of
        // anything: the key is what names the factory or the method,
        // whatever the spec binds the value to. A computed key held in
        // a variable names nothing the rule can see, the one form it
        // leaves alone like the bracket member does; a literal one
        // names the same member the dot form would. A newPage
        // destructured off the fixture context is the one clean form,
        // a page of it being watched like any other.
        for (const property of node.properties) {
          if (property.type !== "Property") continue;
          const key = property.key;
          if (property.computed && key.type !== "Literal") continue;
          const keyName =
            key.type === "Identifier"
              ? key.name
              : key.type === "Literal" && typeof key.value === "string"
                ? key.value
                : null;
          if (keyName === null) continue;
          if (
            ENGINES.has(keyName) ||
            (CREATION_METHODS.has(keyName) &&
              !(
                keyName === "newPage" &&
                source !== null &&
                isFixtureContext(source)
              ))
          ) {
            context.report({ node: property, messageId: "unguardedCreation" });
          }
        }
      },
      ImportDeclaration(node: ESTree.ImportDeclaration) {
        // A type import carries no value to create anything with; the
        // TypeScript parser marks it `importKind: "type"`, which the
        // estree types predate.
        const importKind = (node as { importKind?: string }).importKind;
        if (own || importKind === "type") return;
        if (
          node.source.type !== "Literal" ||
          !SOURCES.has(String(node.source.value))
        ) {
          return;
        }
        for (const specifier of node.specifiers) {
          const specifierKind = (specifier as { importKind?: string })
            .importKind;
          if (
            specifier.type === "ImportSpecifier" &&
            specifierKind !== "type" &&
            specifier.imported.type === "Identifier" &&
            IMPORTS.has(specifier.imported.name)
          ) {
            context.report({ node: specifier, messageId: "unguardedImport" });
          }
        }
      },
    };
  },
};

export default noRequestShortcut;
