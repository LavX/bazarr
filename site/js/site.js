/* ===========================================================================
   Site behaviour.

   Everything here is an enhancement: with JavaScript off the page reads, the
   FAQ's first answer is open, the code blocks are selectable, and the
   screenshots are still images with real alt text.
   =========================================================================== */
(function () {
  "use strict";

  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* -------------------------------------------------------------------------
     FAQ

     The answer animates between grid-template-rows 0fr and 1fr, so it resolves
     to its own intrinsic height. A measured max-height set once at open time
     is wrong the moment the viewport changes, and it silently clips the answer
     that matters most, the migration one, on a phone that rotates.

     Collapsed answers are `visibility: hidden` in CSS, which is what keeps
     their links out of the tab order.
     ------------------------------------------------------------------------- */
  var items = Array.prototype.slice.call(document.querySelectorAll(".faq-item"));

  function setOpen(item, open) {
    item.classList.toggle("open", open);
    var trigger = item.querySelector(".faq-trigger");
    if (trigger) trigger.setAttribute("aria-expanded", open ? "true" : "false");
  }

  items.forEach(function (item) {
    var trigger = item.querySelector(".faq-trigger");
    if (!trigger) return;
    trigger.addEventListener("click", function () {
      setOpen(item, !item.classList.contains("open"));
    });
  });

  /* The FAQ ships as FAQPage structured data, so search engines deep-link
     these anchors directly. Landing on a collapsed answer is a dead end. */
  /* The pre-redesign page numbered these faq-q1 / faq-a1; Google still has
     those in its index. Translate rather than break. */
  function resolveLegacy(id) {
    var m = /^faq-([qa]\d+)$/.exec(id);
    return m ? m[1] : id;
  }

  function openFromHash() {
    var id = resolveLegacy(window.location.hash.slice(1));
    if (!id) return;
    var target = document.getElementById(id);
    if (!target) return;
    var item = target.closest(".faq-item");
    if (item) {
      setOpen(item, true);
      item.scrollIntoView({ block: "center", behavior: reduced ? "auto" : "smooth" });
    }
  }
  window.addEventListener("hashchange", openFromHash);
  openFromHash();

  /* -------------------------------------------------------------------------
     Copy buttons
     ------------------------------------------------------------------------- */
  function flash(btn, cls, label) {
    var original = btn.textContent;
    btn.textContent = label;
    btn.classList.add(cls);
    window.setTimeout(function () {
      btn.textContent = original;
      btn.classList.remove(cls);
    }, 1800);
  }

  document.querySelectorAll(".copy").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var text = btn.dataset.copy;
      if (!text && btn.dataset.copyFrom) {
        var src = document.getElementById(btn.dataset.copyFrom);
        if (src) text = src.textContent;
      }
      if (!text) return;

      var done = function () { flash(btn, "ok", "Copied"); };
      var failed = function () { flash(btn, "fail", "Press Ctrl+C"); };

      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(done, failed);
      } else {
        /* http on a LAN is the normal case for this audience, and the
           clipboard API is unavailable there */
        try {
          var ta = document.createElement("textarea");
          ta.value = text;
          ta.setAttribute("readonly", "");
          ta.style.position = "fixed";
          ta.style.opacity = "0";
          document.body.appendChild(ta);
          ta.select();
          document.execCommand("copy") ? done() : failed();
          document.body.removeChild(ta);
        } catch (e) {
          failed();
        }
      }
    });
  });

  /* -------------------------------------------------------------------------
     Lightbox

     Built as a real dialog: the triggers are buttons, so they are reachable by
     keyboard; the closed dialog is display:none, so nothing inside it can hold
     focus; focus moves in on open, is trapped while open, and returns to the
     trigger on close.
     ------------------------------------------------------------------------- */
  var lightbox = document.getElementById("lightbox");
  if (lightbox) {
    var closeBtn = lightbox.querySelector(".lightbox-close");
    var zoomBtn = lightbox.querySelector(".lightbox-zoom");
    var stage = lightbox.querySelector(".lightbox-stage");
    var caption = lightbox.querySelector(".lightbox-caption");
    var opener = null;

    /* Fit by default, so the whole frame is legible at a glance. Zoom renders
       the capture at its own size and pans, which is the only way a 1920px
       wide UI screenshot is readable on a phone. */
    function setZoom(on) {
      lightbox.classList.toggle("zoomed", on);
      if (zoomBtn) {
        zoomBtn.setAttribute("aria-pressed", on ? "true" : "false");
        zoomBtn.textContent = on ? "Fit" : "Zoom";
      }
      if (!on && stage) { stage.scrollTop = 0; stage.scrollLeft = 0; }
    }
    var shell = [document.querySelector("header.nav"), document.getElementById("main"), document.querySelector("footer")];

    function focusables() {
      return Array.prototype.slice.call(
        lightbox.querySelectorAll('button, [href], img[tabindex="0"]')
      ).filter(function (el) { return el.offsetParent !== null; });
    }

    var scrollLock = null;

    /* `inert` blocks focus and interaction but is not a scroll lock, and a
       fixed overlay does not stop the document behind it moving. Without this,
       wheeling over the control bar or the padding scrolls the page, and
       closing the dialog leaves the reader somewhere else entirely. The
       scrollbar's width is paid back as padding so the layout does not jump. */
    function lockScroll() {
      var gap = window.innerWidth - document.documentElement.clientWidth;
      scrollLock = { overflow: document.body.style.overflow, pad: document.body.style.paddingRight };
      document.body.style.overflow = "hidden";
      if (gap > 0) document.body.style.paddingRight = gap + "px";
    }

    function unlockScroll() {
      if (!scrollLock) return;
      document.body.style.overflow = scrollLock.overflow;
      document.body.style.paddingRight = scrollLock.pad;
      scrollLock = null;
    }

    function open(btn) {
      var src = btn.dataset.full;
      if (!src) return;
      opener = btn;

      var img = lightbox.querySelector("img");
      if (!img) {
        img = document.createElement("img");
        img.addEventListener("click", function () {
          setZoom(!lightbox.classList.contains("zoomed"));
        });
        stage.appendChild(img);
      }
      var inner = btn.querySelector("img");
      img.src = src;
      img.alt = inner ? inner.alt : "";
      /* The alt text already describes the capture, so it doubles as the
         caption rather than being written twice and drifting apart. */
      if (caption) caption.textContent = img.alt;

      setZoom(false);
      lockScroll();
      lightbox.setAttribute("open", "");
      shell.forEach(function (el) { if (el) el.setAttribute("inert", ""); });
      if (closeBtn) closeBtn.focus();
      document.addEventListener("keydown", onKey);
    }

    function close() {
      setZoom(false);
      unlockScroll();
      lightbox.removeAttribute("open");
      shell.forEach(function (el) { if (el) el.removeAttribute("inert"); });
      document.removeEventListener("keydown", onKey);
      if (opener) { opener.focus(); opener = null; }
    }

    function onKey(e) {
      if (e.key === "Escape") { close(); return; }
      if (e.key !== "Tab") return;
      var list = focusables();
      if (!list.length) return;
      var first = list[0];
      var last = list[list.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }

    document.querySelectorAll("[data-full]").forEach(function (btn) {
      btn.addEventListener("click", function () { open(btn); });
    });
    if (closeBtn) closeBtn.addEventListener("click", close);
    if (zoomBtn) {
      zoomBtn.addEventListener("click", function () {
        setZoom(!lightbox.classList.contains("zoomed"));
      });
    }
    /* Clicking the backdrop closes. The stage counts as backdrop, but only
       where the image is not: clicking the image itself toggles zoom. */
    lightbox.addEventListener("click", function (e) {
      if (e.target === lightbox || e.target === stage) close();
    });
  }

  /* -------------------------------------------------------------------------
     Reveal and nav state
     ------------------------------------------------------------------------- */
  /* site.js is here, so the no-JS failsafe in the document head must not fire */
  var fallback = document.documentElement.dataset.revealFallback;
  if (fallback) {
    window.clearTimeout(Number(fallback));
    delete document.documentElement.dataset.revealFallback;
  }

  var revealables = document.querySelectorAll(".reveal");
  if (reduced || !("IntersectionObserver" in window)) {
    revealables.forEach(function (el) { el.classList.add("seen"); });
  } else {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("seen");
          io.unobserve(entry.target);
        }
      });
    }, { rootMargin: "0px 0px -12% 0px" });
    revealables.forEach(function (el) { io.observe(el); });
  }

  /* aria-current follows the section actually in view, rather than being
     hard-coded on one link in the markup */
  var navLinks = Array.prototype.slice.call(document.querySelectorAll('.nav a[href^="#"]'));
  if (navLinks.length && "IntersectionObserver" in window) {
    var sections = navLinks
      .map(function (a) { return document.querySelector(a.getAttribute("href")); })
      .filter(Boolean);

    var spy = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        navLinks.forEach(function (a) {
          var on = a.getAttribute("href") === "#" + entry.target.id;
          if (on) a.setAttribute("aria-current", "page");
          else a.removeAttribute("aria-current");
        });
      });
    }, { rootMargin: "-45% 0px -50% 0px" });

    sections.forEach(function (s) { spy.observe(s); });
  }
})();

/* ===========================================================================
   Guide table of contents.

   The audit found a 4,495px guide with six h2 sections and no way to see them
   at once. The depth key from the hero is the same component: a set of named
   planes with the active one lit, so it becomes the rail here.
   =========================================================================== */
(function () {
  "use strict";

  var rail = document.getElementById("guide-rail");
  var article = document.querySelector(".content");
  if (!rail || !article) return;

  var heads = Array.prototype.slice.call(article.querySelectorAll("h2[id]"));
  if (heads.length < 2) return;   /* a rail over one section is furniture */

  heads.forEach(function (h) {
    var a = document.createElement("a");
    a.href = "#" + h.id;
    a.textContent = h.textContent.trim();
    rail.appendChild(a);
  });

  var links = Array.prototype.slice.call(rail.querySelectorAll("a"));

  if (!("IntersectionObserver" in window)) return;
  var spy = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (!entry.isIntersecting) return;
      links.forEach(function (a) {
        a.classList.toggle("here", a.getAttribute("href") === "#" + entry.target.id);
      });
    });
  }, { rootMargin: "-90px 0px -70% 0px" });

  heads.forEach(function (h) { spy.observe(h); });
})();
