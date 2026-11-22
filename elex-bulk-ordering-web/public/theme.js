// Apply the stored theme before first paint so there is no flash of the
// wrong mode. The toggle itself lives in src/components/Layout.tsx.
//
// A file of its own rather than inline in index.html: the page's
// Content-Security-Policy (frontend/nginx.conf) runs only scripts served by
// the app itself, which keeps an injected script from running at all.
(function () {
  try {
    var t = localStorage.getItem("ordering.theme");
    if (t === "light" || t === "dark") {
      document.documentElement.setAttribute("data-theme", t);
    }
  } catch (e) {
    /* storage can be blocked — the OS preference then wins */
  }
})();
