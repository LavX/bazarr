/* ===========================================================================
   The hero is a shot you can operate.

   A multiplane camera works because the planes travel at different rates, so
   the playhead is the only input: it drives the parallax, the drifting air and
   the subtitle cues, which appear and disappear on their own real timecodes.
   The lane keys drop a language out of the combined subtitle, which is the
   most direct demonstration this product has of what it adds.

   Nothing here is load-bearing. With JavaScript off, p5 missing, or motion
   reduced, the plate is correct in CSS and the first cue stands open.
   =========================================================================== */
(function () {
  "use strict";

  var hero = document.querySelector(".hero");
  if (!hero) return;

  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  var range = document.getElementById("playhead");
  var readout = document.getElementById("playhead-time");
  var cues = Array.prototype.slice.call(hero.querySelectorAll("[data-cue]"));
  var lines = Array.prototype.slice.call(hero.querySelectorAll(".cel"));
  var keys = Array.prototype.slice.call(hero.querySelectorAll("button[data-track]"));

  var DURATION = 6;
  var t = 2.4;              /* seconds into the shot */
  var playing = !reduced;
  var enabled = { dialogue: true, translated: true };

  /* --- timecode --------------------------------------------------------- */
  function timecode(sec) {
    var ms = Math.floor((sec % 1) * 1000);
    var s = Math.floor(sec) % 60;
    var m = Math.floor(sec / 60) % 60;
    var pad = function (n, w) { return String(n).padStart(w, "0"); };
    return "00:" + pad(m, 2) + ":" + pad(s, 2) + "," + pad(ms, 3);
  }

  /* --- which cues are on screen at t ------------------------------------ */
  function paint() {
    cues.forEach(function (cue) {
      var inAt = parseFloat(cue.dataset.in || "0");
      var outAt = parseFloat(cue.dataset.out || String(DURATION));
      cue.classList.toggle("is-on", t >= inAt && t <= outAt);
    });

    /* The lane keys drop a language out of the combined subtitle rather than
       hiding the cue, which is the distinction the product actually makes. */
    lines.forEach(function (cel) {
      var track = cel.classList.contains("translated") ? "translated" : "dialogue";
      cel.classList.toggle("is-off", !enabled[track]);
    });

    keys.forEach(function (btn) {
      var on = enabled[btn.dataset.track];
      btn.classList.toggle("off", !on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    });

    if (readout) readout.textContent = timecode(t);
    if (range && Math.abs(parseFloat(range.value) - t) > 0.01) range.value = String(t);
  }

  /* --- controls --------------------------------------------------------- */
  if (range) {
    range.max = String(DURATION);
    range.addEventListener("input", function () {
      playing = false;          /* taking hold of the playhead stops playback */
      t = parseFloat(range.value);
      paint();
    });
  }

  keys.forEach(function (btn) {
    btn.addEventListener("click", function () {
      var track = btn.dataset.track;
      enabled[track] = !enabled[track];
      paint();
    });
  });

  /* Dragging anywhere across the plate scrubs it, which is what makes the
     shot feel like a shot rather than a slider with a picture behind it. */
  var dragging = false;
  function scrubFromEvent(e) {
    var r = hero.getBoundingClientRect();
    var x = ((e.touches ? e.touches[0].clientX : e.clientX) - r.left) / r.width;
    t = Math.max(0, Math.min(DURATION, x * DURATION));
    paint();
  }
  hero.addEventListener("pointerdown", function (e) {
    if (e.target.closest("a, button, input, .stats, code")) return;
    dragging = true;
    playing = false;
    hero.classList.add("scrubbing");
    scrubFromEvent(e);
  });
  window.addEventListener("pointermove", function (e) { if (dragging) scrubFromEvent(e); });
  window.addEventListener("pointerup", function () {
    dragging = false;
    hero.classList.remove("scrubbing");
  });

  paint();

  /* -------------------------------------------------------------------------
     The subtitle track, in p5.

     The ambient motion behind the hero is a Remotion loop, so p5 is not
     duplicating it. Its job here is the part a pre-rendered video cannot do:
     answer the playhead. The waveform, the two cue lanes and the playhead all
     redraw as you scrub, which is what makes the shot feel operated rather
     than played at you.
     ------------------------------------------------------------------------- */
  var host = document.getElementById("track");
  if (!host || typeof window.p5 !== "function") return;

  /* The audio, and the subtitles that must agree with it.

     SPEECH is the thing being described: two lines of dialogue with a beat of
     room tone before, between and after them. Everything the track draws is
     derived from these numbers, so a cue can never sit over silence, which is
     precisely the failure the product exists to fix. `rate` is syllables per
     second, and it is what makes a burst read as talking rather than as a
     block of noise. The cue times match the data-in/data-out on the cels. */
  var SPEECH = [
    { at: 0.55, to: 2.20, rate: 3.6 },
    { at: 3.30, to: 5.35, rate: 4.2 }
  ];

  var CUES = [
    { lane: 0, at: 0.55, to: 2.55, label: "You need to see this." },
    { lane: 1, at: 0.55, to: 2.55, label: "Ezt l\u00e1tnod kell." },
    { lane: 0, at: 3.30, to: 5.65, label: "It\u2019s why I called you." },
    { lane: 1, at: 3.30, to: 5.65, label: "Ez\u00e9rt h\u00edvtalak." }
  ];

  new window.p5(function (p) {
    var W = 0, H = 0, wave = [];
    var CREAM = "#fff8e1", AMBER = "#e68a00", AMBER_BRIGHT = "#ffb347";

    function layout() {
      var r = host.getBoundingClientRect();
      W = Math.max(1, Math.round(r.width));
      H = Math.max(1, Math.round(r.height));
    }

    /* Room tone. Not digital silence: a real track still has a floor, and a
       flat zero line would read as a broken canvas rather than as quiet. */
    var FLOOR = 0.04;

    /* How loud the speaker is at a given second, 0 when nobody is talking. */
    function loudnessAt(sec) {
      for (var k = 0; k < SPEECH.length; k++) {
        var s = SPEECH[k];
        if (sec < s.at || sec > s.to) continue;
        var into = sec - s.at;
        var span = s.to - s.at;

        /* the mouth opening and closing, never all the way shut mid-word */
        var syllable = 0.5 + 0.5 * Math.pow(Math.abs(Math.sin(into * Math.PI * s.rate)), 0.55);
        /* a spoken phrase rises off its first word and falls away at the end */
        var phrase = 0.66 + 0.34 * Math.sin((into / span) * Math.PI);
        /* and it does not switch on at full level */
        var edge = Math.min(1, into / 0.07) * Math.min(1, (s.to - sec) / 0.13);

        return syllable * phrase * edge;
      }
      return 0;
    }

    /* Deterministic, so the track is the same shape on every visit. */
    function buildWave() {
      wave = [];
      var seed = 20260826;
      var rnd = function () {
        seed = (seed * 1103515245 + 12345) & 0x7fffffff;
        return seed / 0x7fffffff;
      };
      var N = 220;
      for (var i = 0; i < N; i++) {
        var loud = loudnessAt(((i + 0.5) / N) * DURATION);
        /* the jitter is scaled by loudness, so quiet stays quiet */
        wave.push(FLOOR * (0.55 + 0.9 * rnd()) + loud * (0.66 + 0.34 * rnd()));
      }
    }

    p.setup = function () {
      layout();
      var c = p.createCanvas(W, H);
      c.parent(host);
      c.canvas.setAttribute("aria-hidden", "true");
      p.pixelDensity(Math.min(window.devicePixelRatio || 1, 2));
      p.noStroke();
      buildWave();
      p.frameRate(30);
    };

    p.windowResized = function () { layout(); p.resizeCanvas(W, H); };

    function x(sec) { return (sec / DURATION) * W; }

    p.draw = function () {
      if (playing) {
        t += p.deltaTime / 1000;
        if (t > DURATION) t = 0;
        paint();
      }
      p.clear();

      var waveTop = H * 0.06, waveH = H * 0.36, mid = waveTop + waveH / 2;
      var bw = W / wave.length;
      var barW = Math.max(2, bw * 0.62);

      /* The audio the subtitles must agree with. Quiet stretches collapse to a
         thin line, so the two spoken bursts are the only loud thing here and
         the cue bars below them line up with them by eye. */
      for (var i = 0; i < wave.length; i++) {
        var h = Math.max(1.5, wave[i] * waveH);
        var played = (i + 0.5) * bw <= x(t);
        p.fill(played ? "rgba(255,248,225,0.34)" : "rgba(255,248,225,0.14)");
        p.rect(i * bw + (bw - barW) / 2, mid - h / 2, barW, h, 1);
      }

      /* One lane per language, set close enough to read as a single combined
         subtitle rather than two unrelated tracks. */
      var laneH = H * 0.19, gap = H * 0.022;
      var laneTop = H * 0.54;
      var laneY = [laneTop, laneTop + laneH + gap];

      for (var lane = 0; lane < 2; lane++) {
        p.fill(enabled[lane === 0 ? "dialogue" : "translated"]
          ? "rgba(255,248,225,0.05)"
          : "rgba(255,248,225,0.02)");
        p.rect(0, laneY[lane], W, laneH, 3);
      }

      p.textSize(Math.max(10, H * 0.058));
      p.textAlign(p.LEFT, p.CENTER);
      for (var j = 0; j < CUES.length; j++) {
        var cue = CUES[j];
        var on = enabled[cue.lane === 0 ? "dialogue" : "translated"];
        if (!on) continue;
        var y = laneY[cue.lane];
        var live = t >= cue.at && t <= cue.to;

        p.fill(cue.lane === 0
          ? (live ? "rgba(255,248,225,0.22)" : "rgba(255,248,225,0.09)")
          : (live ? "rgba(230,138,0,0.34)" : "rgba(230,138,0,0.13)"));
        p.rect(x(cue.at), y, x(cue.to) - x(cue.at), laneH, 3);

        /* clipped to its own bar, so a long line cannot spill across the
           silence into the next cue and undo the alignment being shown */
        p.drawingContext.save();
        p.drawingContext.beginPath();
        p.drawingContext.rect(x(cue.at), y, x(cue.to) - x(cue.at) - 6, laneH);
        p.drawingContext.clip();
        p.fill(cue.lane === 0 ? CREAM : AMBER_BRIGHT);
        p.text(cue.label, x(cue.at) + 9, y + laneH / 2 + 1);
        p.drawingContext.restore();
      }

      /* the playhead */
      var px = x(t);
      p.fill(AMBER_BRIGHT);
      p.rect(px - 1, 0, 2, H);
      p.triangle(px - 6, 0, px + 6, 0, px, 8);
    };
  });
})();
