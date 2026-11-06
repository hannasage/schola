/** @typedef {'hub' | 'home' | 'library' | 'curriculum' | 'experiments' | 'guide' | 'sync'} WorkspaceView */
/** @typedef {{view:WorkspaceView,ready:boolean,navigating:boolean,progress:{done:number,total:number}|null}} ShellSnapshot */
/** @typedef {{navigate:(view:WorkspaceView)=>Promise<boolean>,exportProgress:()=>void,restoreBackup:()=>void,reportError:(error:Error)=>void}} ShellCommands */
export function createShellBridge() {
  /** @type {ShellSnapshot} */ let snapshot = {view:'curriculum', ready:false, navigating:false, progress:null};
  /** @type {ShellCommands | undefined} */ let commands;
  /** @type {Set<()=>void>} */ const listeners = new Set();
  /** @param {Partial<ShellSnapshot>} next */
  function update(next) { snapshot = {...snapshot, ...next}; for (const listener of listeners) listener(); }
  return {
    getSnapshot: () => snapshot,
    /** @param {()=>void} listener */
    subscribe(listener) { listeners.add(listener); return () => { listeners.delete(listener); }; },
    /** @param {ShellCommands} next */ bind(next) { commands = next; },
    update,
    /** @param {WorkspaceView} view */
    async navigate(view) {
      if (!commands || snapshot.navigating) return false;
      update({navigating:true});
      try {
        if (!(await commands.navigate(view))) return false;
        update({view});
        return true;
      } catch (error) { commands.reportError(error instanceof Error ? error : new Error(String(error))); return false; }
      finally { update({navigating:false}); }
    },
    exportProgress() { if (snapshot.ready) commands?.exportProgress(); },
    restoreBackup() { if (snapshot.ready) commands?.restoreBackup(); },
  };
}
export const shell = createShellBridge();
