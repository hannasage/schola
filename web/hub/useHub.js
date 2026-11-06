import {useCallback, useEffect, useRef, useState} from 'react';

/**
 * @typedef {{status: 'idle' | 'loading' | 'ready' | 'error', hub: HubResponse | null, error: Error | null}} HubState
 */

/**
 * Loads the hub numbers each time the screen becomes active. A newer request wins; an older answer is dropped.
 * Numbers from an earlier visit stay on screen while a new request runs. A failure replaces them with the error.
 * @param {boolean} active
 * @param {{getHub: (options?: {signal?: AbortSignal}) => Promise<HubResponse>}} service
 * @returns {{state: HubState, reload: () => void}}
 */
export function useHub(active, service) {
  const [state, setState] = useState(/** @type {HubState} */({status: 'idle', hub: null, error: null}));
  const latest = useRef(0);
  const controller = useRef(/** @type {AbortController | null} */(null));

  const reload = useCallback(() => {
    controller.current?.abort();
    const abort = new AbortController();
    controller.current = abort;
    const id = ++latest.current;
    setState(previous => ({status: previous.hub ? 'ready' : 'loading', hub: previous.hub, error: null}));
    service.getHub({signal: abort.signal}).then(
      hub => { if (id === latest.current) setState({status: 'ready', hub, error: null}); },
      error => { if (id === latest.current) setState({status: 'error', hub: null, error: error instanceof Error ? error : new Error(String(error))}); },
    );
  }, [service]);

  useEffect(() => {
    if (active) reload();
    return () => { controller.current?.abort(); latest.current += 1; };
  }, [active, reload]);

  return {state, reload};
}
