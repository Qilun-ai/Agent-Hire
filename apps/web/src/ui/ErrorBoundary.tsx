// Keeps one broken view from blanking the whole app, and reports the error to the API log.
import { Component, type ComponentChildren } from "preact";
import type { ApiFetch } from "../types";

interface Props {
  label: string;
  api?: ApiFetch | null;
  children: ComponentChildren;
}

export default class ErrorBoundary extends Component<Props, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error) {
    const { api, label } = this.props;
    api?.("/api/v1/errors", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: `web:${label}`, message: error.message, stack: error.stack?.slice(0, 4000) }),
    }).catch(() => {});
  }

  componentDidUpdate(previous: Props) {
    if (previous.label !== this.props.label && this.state.error) this.setState({ error: null });
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="view-error" role="alert">
        <h2>{this.props.label} hit a problem</h2>
        <p>{error.message || "Something went wrong while showing this page."}</p>
        <button className="btn" onClick={() => this.setState({ error: null })}>Try again</button>
      </div>
    );
  }
}
