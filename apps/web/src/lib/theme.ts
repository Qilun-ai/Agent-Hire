// Light / dark / follow-the-OS theme, stored per browser.
export type ThemeChoice = "light" | "dark" | "system";

const KEY = "agent-hire-theme";

export function readTheme(): ThemeChoice {
  try {
    const stored = localStorage.getItem(KEY);
    return stored === "light" || stored === "dark" ? stored : "system";
  } catch {
    return "system";
  }
}

function apply(choice: ThemeChoice) {
  const dark = choice === "dark" || (choice === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
}

export function setTheme(choice: ThemeChoice) {
  try {
    localStorage.setItem(KEY, choice);
  } catch {
    /* private window: the choice lasts for this page only */
  }
  apply(choice);
}

/** Keeps "system" in step with the OS when it switches between light and dark. */
export function followSystemTheme(): () => void {
  const query = matchMedia("(prefers-color-scheme: dark)");
  const onChange = () => {
    if (readTheme() === "system") apply("system");
  };
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}
