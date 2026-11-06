import {Component} from 'react';
import {Alert, Button} from '@hannasage/projection-ui/core';

/**
 * Catches a crash while the Hub draws, so the rest of the app keeps working.
 * It shows a plain message and a button that clears the crash and loads the hub again.
 * @extends {Component<{children: import('react').ReactNode, onRetry: () => void, resetKey?: unknown}, {failed: boolean}>}
 */
export class HubBoundary extends Component {
  state = {failed: false};
  static getDerivedStateFromError() { return {failed: true}; }
  /** @param {Error} error */
  componentDidCatch(error) { console.error('The hub screen could not draw:', error); }
  /** @param {{resetKey?: unknown}} previous */
  componentDidUpdate(previous) {
    // New numbers arrived: try to draw them. A crash that comes from the same numbers shows the message again.
    if (this.state.failed && previous.resetKey !== this.props.resetKey) this.setState({failed: false});
  }
  retry = () => { this.setState({failed: false}); this.props.onRetry(); };
  render() {
    if (!this.state.failed) return this.props.children;
    return <div className="hub-state" data-hub-state="crashed">
      <Alert tone="danger" title="Schola cannot show the hub">
        <p>Something went wrong while the hub was drawn. The rest of Schola still works.</p>
      </Alert>
      <div><Button variant="primary" data-hub-retry onClick={this.retry}>Try again</Button></div>
    </div>;
  }
}
