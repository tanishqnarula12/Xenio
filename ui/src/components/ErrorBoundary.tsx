import { Component, type ErrorInfo, type ReactNode } from "react";

// One part that cannot be drawn must not take the page with it: without this React empties the whole window (seen in a test:
// a lesson went black after one bad answer from the server). What failed says so, and can be tried again.
export class ErrorBoundary extends Component<{ children: ReactNode; what?: string; resetKey?: unknown }, { failed: boolean }> {
  override state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  override componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(this.props.what ?? "part of the page", error.message, info.componentStack);
  }

  override componentDidUpdate(previous: { resetKey?: unknown }) {
    if (this.state.failed && previous.resetKey !== this.props.resetKey) this.setState({ failed: false });
  }

  override render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div className="unavailable" role="alert">
        <p>{this.props.what ?? "This part"} could not be shown.</p>
        <button type="button" className="btn btn--quiet" onClick={() => this.setState({ failed: false })}>Try again</button>
      </div>
    );
  }
}
