// Intersection Observer for fade-in sections
(function () {
  var sections = document.querySelectorAll('.fade-section');
  if (!window.IntersectionObserver) {
    sections.forEach(function (s) { s.classList.add('visible'); });
    return;
  }
  var observer = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (entry.isIntersecting) {
        entry.target.classList.add('visible');
        observer.unobserve(entry.target);
      }
    });
  }, { threshold: 0.1 });
  sections.forEach(function (s) { observer.observe(s); });
})();

// Copy buttons with visual feedback
document.addEventListener('click', function (e) {
  var btn = e.target.closest('.copy-btn');
  if (!btn) return;

  // Guard against rapid clicks
  if (btn.hasAttribute('data-copying')) return;
  btn.setAttribute('data-copying', '');

  var text = btn.getAttribute('data-copy');
  var sourceId;
  if (!text) {
    sourceId = btn.getAttribute('data-copy-from');
    if (sourceId) {
      var el = document.getElementById(sourceId);
      text = el ? el.textContent : '';
    }
  }
  if (!text) { btn.removeAttribute('data-copying'); return; }

  // Store original label once
  if (!btn.hasAttribute('data-label')) {
    btn.setAttribute('data-label', btn.textContent);
  }
  var orig = btn.getAttribute('data-label');

  if (!navigator.clipboard || !navigator.clipboard.writeText) {
    // Fallback: select text and exec copy
    try {
      var range = document.createRange();
      var sel = window.getSelection();
      var sourceEl = sourceId ? document.getElementById(sourceId) : null;
      if (sourceEl) {
        range.selectNodeContents(sourceEl);
      } else {
        var tmp = document.createElement('textarea');
        tmp.value = text;
        tmp.style.position = 'fixed';
        tmp.style.opacity = '0';
        document.body.appendChild(tmp);
        tmp.select();
        document.execCommand('copy');
        document.body.removeChild(tmp);
        btn.textContent = 'Copied!';
        btn.classList.add('copied');
        setTimeout(function () {
          btn.textContent = orig;
          btn.classList.remove('copied');
          btn.removeAttribute('data-copying');
        }, 1500);
        return;
      }
      sel.removeAllRanges();
      sel.addRange(range);
      document.execCommand('copy');
      sel.removeAllRanges();
      btn.textContent = 'Copied!';
      btn.classList.add('copied');
      setTimeout(function () {
        btn.textContent = orig;
        btn.classList.remove('copied');
        btn.removeAttribute('data-copying');
      }, 1500);
    } catch (err) {
      btn.removeAttribute('data-copying');
    }
    return;
  }

  navigator.clipboard.writeText(text).then(function () {
    btn.textContent = 'Copied!';
    btn.classList.add('copied');
    setTimeout(function () {
      btn.textContent = orig;
      btn.classList.remove('copied');
      btn.removeAttribute('data-copying');
    }, 1500);
  }).catch(function () {
    btn.textContent = 'Failed';
    btn.classList.add('copy-fail');
    setTimeout(function () {
      btn.textContent = orig;
      btn.classList.remove('copy-fail');
      btn.removeAttribute('data-copying');
    }, 1500);
  });
});

// FAQ accordion
(function () {
  document.addEventListener('click', function (e) {
    var trigger = e.target.closest('.faq-trigger');
    if (!trigger) return;

    var item = trigger.closest('.faq-item');
    var answer = item.querySelector('.faq-answer');
    var isOpen = item.classList.contains('open');

    // Close all others
    document.querySelectorAll('.faq-item.open').forEach(function (other) {
      if (other !== item) {
        other.classList.remove('open');
        other.querySelector('.faq-trigger').setAttribute('aria-expanded', 'false');
        other.querySelector('.faq-answer').style.maxHeight = '0';
      }
    });

    if (isOpen) {
      item.classList.remove('open');
      trigger.setAttribute('aria-expanded', 'false');
      answer.style.maxHeight = '0';
    } else {
      item.classList.add('open');
      trigger.setAttribute('aria-expanded', 'true');
      answer.style.maxHeight = answer.scrollHeight + 'px';
    }
  });
})();

// Screenshot lightbox
(function () {
  var lightbox = document.getElementById('lightbox');
  if (!lightbox) return;

  var lbImg = lightbox.querySelector('img');
  var frames = document.querySelectorAll('.screenshot-frame');

  frames.forEach(function (frame) {
    frame.addEventListener('click', function () {
      var img = frame.querySelector('img');
      if (!img) return;
      // Use currentSrc (browser-selected source from <picture>), but prefer
      // the full-resolution variant by extracting the base name from srcset
      var source = frame.querySelector('source[type="image/webp"]');
      var fullSrc = null;
      if (source) {
        var srcset = source.getAttribute('srcset');
        var parts = srcset.split(',');
        // Pick the largest variant (last entry in srcset)
        var last = parts[parts.length - 1].trim().split(/\s+/)[0];
        if (last) fullSrc = last;
      }
      lbImg.src = fullSrc || img.currentSrc || img.src;
      lbImg.alt = img.alt;
      lightbox.classList.add('open');
      lightbox.setAttribute('aria-hidden', 'false');
      document.body.style.overflow = 'hidden';
    });
  });

  function closeLightbox() {
    lightbox.classList.remove('open');
    lightbox.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
  }

  lightbox.addEventListener('click', function (e) {
    if (e.target === lightbox || e.target.closest('.lightbox-close')) {
      closeLightbox();
    }
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && lightbox.classList.contains('open')) {
      closeLightbox();
    }
  });
})();

// Scroll spy for nav
(function () {
  var nav = document.querySelector('.sticky-nav');
  if (!nav) return;

  var links = nav.querySelectorAll('a[href^="#"]');
  if (!links.length) return;

  var sections = [];
  links.forEach(function (link) {
    var id = link.getAttribute('href').slice(1);
    var target = document.getElementById(id);
    if (target) {
      // Walk up to the parent section for accurate offset measurement
      var section = target.closest('section') || target;
      sections.push({ el: section, link: link });
    }
  });

  if (!sections.length) return;

  var homeLink = nav.querySelector('a[href="./"]');

  function update() {
    var scrollY = window.scrollY + 120;
    var active = null;

    for (var i = sections.length - 1; i >= 0; i--) {
      if (sections[i].el.offsetTop <= scrollY) {
        active = sections[i];
        break;
      }
    }

    links.forEach(function (l) { l.classList.remove('nav-scrollspy', 'nav-active'); });
    if (homeLink) homeLink.classList.remove('nav-active');

    if (active) {
      active.link.classList.add('nav-scrollspy');
    } else if (homeLink) {
      homeLink.classList.add('nav-active');
    }
  }

  var ticking = false;
  window.addEventListener('scroll', function () {
    if (!ticking) {
      requestAnimationFrame(function () { update(); ticking = false; });
      ticking = true;
    }
  }, { passive: true });

  update();
})();
