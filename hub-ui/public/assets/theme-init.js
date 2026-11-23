/*
 * Sets data-theme on <html> before first paint, so a dark page never flashes white. Same rules as
 * src/theme.ts: Dark is the default, System follows the operating system, any failure means dark.
 * A separate file because the CSP (script-src 'self') blocks inline scripts; under /assets/ because
 * the private gateway only passes scripts there.
 */
(function () {
  var choice = null;
  try {
    choice = localStorage.getItem("engineeringHub.theme");
  } catch (error) {
    // Storage unavailable: keep the default.
  }
  var theme = choice === "light" ? "light" : "dark";
  if (choice === "system") {
    try {
      if (!matchMedia("(prefers-color-scheme: dark)").matches) theme = "light";
    } catch (error) {
      // No media query support: keep the default.
    }
  }
  document.documentElement.setAttribute("data-theme", theme);
})();
