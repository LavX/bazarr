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

// Copy buttons with clipboard guard and error handling
document.addEventListener('click', function (e) {
  var btn = e.target.closest('.copy-btn');
  if (!btn) return;

  var text = btn.getAttribute('data-copy');
  var sourceId;
  if (!text) {
    sourceId = btn.getAttribute('data-copy-from');
    if (sourceId) {
      var el = document.getElementById(sourceId);
      text = el ? el.textContent : '';
    }
  }
  if (!text) return;

  if (!navigator.clipboard || !navigator.clipboard.writeText) {
    // Fallback: select text for manual copy
    var range = document.createRange();
    var sel = window.getSelection();
    if (sourceId) {
      var sourceEl = document.getElementById(sourceId);
      if (sourceEl) {
        range.selectNodeContents(sourceEl);
        sel.removeAllRanges();
        sel.addRange(range);
      }
    }
    return;
  }

  navigator.clipboard.writeText(text).then(function () {
    var orig = btn.textContent;
    btn.textContent = 'Copied!';
    setTimeout(function () { btn.textContent = orig; }, 1500);
  }).catch(function () {
    btn.textContent = 'Failed';
    setTimeout(function () { btn.textContent = 'Copy'; }, 1500);
  });
});
