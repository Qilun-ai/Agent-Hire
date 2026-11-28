import { render } from "preact";
import App from "./App";
import { followSystemTheme } from "./lib/theme";
import "@fontsource-variable/inter";
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/700.css";
import "./styles/base.css";
import "./styles/agent.css";
import "./styles/pages.css";

followSystemTheme();
render(<App />, document.getElementById("root")!);
