// Set the stored appearance before the stylesheet can paint.
try {
  const theme = localStorage.getItem("agent-hire-theme");
  if (theme === "dark" || theme === "light") document.documentElement.dataset.theme = theme;
} catch {}
