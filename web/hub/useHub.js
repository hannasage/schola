import {useCallback, useEffect, useRef, useState} from 'react';

/**
 * `hub` is the last answer that arrived, or null. A failure always clears it, so old numbers never sit beside an error.
 * `refreshing` is true while a new request runs and old numbers are still on screen.
 * `offsetFallback` is true when the server was sent a clock offset in place of the time zone name.
 * @typedef {{status: 'idle' | 'loading' | 'ready' | 'error', hub: HubResponse | null, error: Error | null, refreshing: boolean, offsetFallback: boolean}} HubState
 */

/**
 * Loads the hub numbers each time the screen becomes active. A newer request wins; an older answer is dropped.
 * Leaving the screen cancels the request that is running.
 * @param {boolean} active
 * @param {import('../services/hub.js').HubService} service
 * @returns {{state: HubState, reload: () => void}}
 */
export function useHub(active, service) {
  const [state, setState] = useState(/** @type {HubState} */({status: 'idle', hub: null, error: null, refreshing: false, offsetFallback: false}));
  const latest = useRef(0);
  const controller = useRef(/** @type {AbortController | null} */(null));

  const reload = useCallback(() => {
    controller.current?.abort();
    const abort = new AbortController();
    controller.current = abort;
    const id = ++latest.current;
    setState(previous => ({status: previous.hub ? 'ready' : 'loading', hub: previous.hub, error: null, refreshing: Boolean(previous.hub), offsetFallback: previous.offsetFallback}));
    service.getHub({signal: abort.signal}).then(
      ({hub, offsetFallback}) => { if (id === latest.current) setState({status: 'ready', hub, error: null, refreshing: false, offsetFallback}); },
      error => { if (id === latest.current) setState({status: 'error', hub: null, error: error instanceof Error ? error : new Error(String(error)), refreshing: false, offsetFallback: false}); },
    );
  }, [service]);

  useEffect(() => {
    if (active) reload();
    return () => { controller.current?.abort(); latest.current += 1; };
  }, [active, reload]);

  return {state, reload};
}
