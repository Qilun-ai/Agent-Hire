// Applies the saved theme before the first paint, so the page never flashes the wrong colours.
(function () {
  try {
    var choice = localStorage.getItem("agent-hire-theme") || "system";
    var dark = choice === "dark" || (choice === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
  } catch (e) {
    document.documentElement.dataset.theme = "dark";
  }
})();
