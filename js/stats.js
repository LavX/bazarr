/* ===========================================================================
   Live project numbers.

   PRODUCT.md allows adoption figures only when they are fetched, never typed
   into copy as a static claim. So nothing here is hard-coded, and anything that
   has never resolved stays an em dash rather than degrading to a guess.

   Three sources, cheapest first:

   1. localStorage, for whatever this browser resolved last. Painted immediately
      on load, so the numbers are there on the first frame instead of flashing
      dashes, and they survive being offline or rate limited.

   2. data/stats.json, same origin, written by .github/workflows/site-stats.yml
      straight onto gh-pages. When it exists, one same-origin request answers
      everything and no third party is contacted at all.

   3. Live fetches, for the window before that workflow has ever run.

   Source 3 is a third-party request, worth stating plainly given the site's own
   no-telemetry claim: it sends the visitor's IP and user agent to GitHub. It
   carries no identifiers of ours and sets no cookie.

   Unauthenticated api.github.com allows 60 requests an hour per IP, which is
   why the cache below gates the network rather than just decorating it: within
   TTL no request is made at all, and a refresh that fails still leaves the last
   good numbers on the page.

   There is deliberately no container pull count. GHCR does not expose one:
   GitHub's API description for the container-versions endpoint has no
   download_count field, so any figure shown would have been invented.
   =========================================================================== */
(function () {
  "use strict";

  var root = document.getElementById("stats");
  var hasVotes = document.querySelector("[data-votes]");
  if (!root && !hasVotes) return;

  var REPO = "LavX/bazarr";
  var KEY = "bazarrplus:stats:1";   /* bump the suffix if the shape changes */
  var TTL = 60 * 1000;              /* refresh at most once a minute */

  var nf = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });

  /* --- storage ----------------------------------------------------------
     Every access is wrapped. localStorage is not merely empty in a private
     window or with site data blocked, the property access itself can throw,
     and a stats widget must never be the thing that breaks the page. */
  function readCache() {
    try {
      var raw = window.localStorage.getItem(KEY);
      if (!raw) return null;
      var c = JSON.parse(raw);
      if (!c || typeof c !== "object" || !c.d || typeof c.d !== "object") return null;
      return c;
    } catch (e) {
      return null;
    }
  }

  function writeCache(data) {
    try {
      window.localStorage.setItem(KEY, JSON.stringify({ v: 1, checked: Date.now(), d: data }));
    } catch (e) {
      /* quota, private mode, blocked storage: the page still works */
    }
  }

  /* `checked` is the last *attempt*, not the last success. Stamping it either
     way is what stops a rate-limited visitor retrying on every page load. */
  function isFresh(c) {
    if (!c || typeof c.checked !== "number") return false;
    var age = Date.now() - c.checked;
    return age >= 0 && age < TTL;   /* a clock moved backwards reads as stale */
  }

  function merge(into, from) {
    if (!from) return into;
    Object.keys(from).forEach(function (k) {
      var v = from[k];
      if (v === undefined || v === null) return;
      if (k === "votes") {
        into.votes = Object.assign({}, into.votes || {}, v);
      } else {
        into[k] = v;
      }
    });
    return into;
  }

  /* --- rendering --------------------------------------------------------- */
  function put(key, value) {
    if (!root || value === null || value === undefined) return;
    var el = root.querySelector('[data-stat="' + key + '"]');
    if (!el) return;
    el.textContent = typeof value === "number" ? nf.format(value) : value;
    el.classList.add("live");
  }

  function putRelease(rel) {
    if (!root || !rel || !rel.tag) return;
    var el = root.querySelector('[data-stat="release"]');
    if (!el) return;
    var a = document.createElement("a");
    a.href = rel.url || "https://github.com/" + REPO + "/releases/tag/" + rel.tag;
    a.rel = "noopener";
    a.textContent = rel.tag;
    el.textContent = "";
    el.appendChild(a);
    if (rel.published) {
      var t = document.createElement("time");
      t.dateTime = rel.published;
      t.textContent = new Date(rel.published).toLocaleDateString(undefined, { month: "short", year: "numeric" });
      el.appendChild(document.createTextNode(" "));
      el.appendChild(t);
    }
    el.classList.add("live");
  }

  /* Upstream upvote counts in the comparison table. Unlike the hero stats these
     sit mid-sentence, so a missing value must leave the last verified number in
     the markup standing rather than blanking to a dash. */
  function putVotes(votes) {
    if (!votes) return;
    document.querySelectorAll("[data-votes]").forEach(function (el) {
      var n = votes[el.dataset.votes];
      if (typeof n !== "number") return;
      el.textContent = String(n);
      el.classList.add("live");
    });
  }

  function render(d) {
    if (!d) return;
    put("stars", d.stars);
    put("forks", d.forks);
    put("providers", d.providers);
    putRelease(d.release);
    putVotes(d.votes);
  }

  function complete(d) {
    return d && typeof d.stars === "number" && typeof d.forks === "number" &&
           typeof d.providers === "number" && d.release && d.release.tag;
  }

  /* --- sources ----------------------------------------------------------- */
  function json(url, opts) {
    return fetch(url, opts)
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
  }

  function fromFile() {
    return json("data/stats.json", { cache: "no-cache" }).then(function (d) {
      if (!d) return null;
      return {
        stars: d.stars,
        forks: d.forks,
        providers: d.providers,
        release: d.release ? { tag: d.release } : undefined,
        votes: d.votes,
      };
    });
  }

  function fromApi() {
    var gh = { headers: { Accept: "application/vnd.github+json" } };
    return Promise.all([
      json("https://api.github.com/repos/" + REPO, gh),
      json("https://api.github.com/repos/" + REPO + "/releases/latest", gh),
      /* the plugin catalog's own manifest, the same file the application reads,
         so the number on the page is the number in the product */
      json("https://raw.githubusercontent.com/" + REPO.split("/")[0] + "/bazarr-provider-catalog/main/catalog.json"),
    ]).then(function (r) {
      var repo = r[0], rel = r[1], cat = r[2];
      var out = {};
      if (repo) { out.stars = repo.stargazers_count; out.forks = repo.forks_count; }
      if (rel && rel.tag_name) {
        out.release = { tag: rel.tag_name, url: rel.html_url, published: rel.published_at };
      }
      if (cat && cat.providers) out.providers = cat.providers.length;
      return out;
    });
  }

  /* --- resolve ----------------------------------------------------------- */
  var cached = readCache();
  if (cached) render(cached.d);
  if (isFresh(cached)) return;                 /* inside TTL: no network at all */

  var data = merge({}, cached && cached.d);

  fromFile()
    .then(function (f) {
      merge(data, f);
      /* the generated file answers everything it can; only what it left out
         costs a third-party request */
      return complete(data) ? null : fromApi();
    })
    .then(function (a) {
      merge(data, a);
      render(data);
    })
    .catch(function () { /* keep whatever the cache already painted */ })
    .then(function () {
      /* stamped on success and failure alike, so a rate-limited or offline
         visitor does not retry on every single page load */
      writeCache(data);
    });
})();
