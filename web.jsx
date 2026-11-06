import {Component, useLayoutEffect, useRef, useState, useSyncExternalStore} from 'react';
import {createRoot} from 'react-dom/client';
import {createPortal} from 'react-dom';
import {ThemeProvider, Button, Badge, Progress} from '@hannasage/projection-ui/core';
import {ThemeSwitcher, useAppearance} from './appearance.jsx';
import {HomeScreen,LibraryScreen} from './web/screens/LearningScreens.jsx';
import {HubScreen} from './web/hub/HubScreen.jsx';
import {shell} from './web/shell/bridge.js';
import {ConnectionScreen} from './web/connection/screen.jsx';
import '@hannasage/projection-ui/styles';

/** @typedef {import('./web/shell/bridge.js').WorkspaceView} WorkspaceView */
/** @typedef {{actions:HTMLElement,navigation:HTMLElement,resume:HTMLElement,progress:HTMLElement,connection:HTMLElement,home:HTMLElement,library:HTMLElement,hub:HTMLElement}} ShellSlots */
/** @param {ParentNode} root @param {string} selector @returns {HTMLElement} */
function element(root, selector) {
  const result = root.querySelector(selector);
  if (!(result instanceof HTMLElement)) throw new Error(`Missing control: ${selector}`);
  return result;
}

/** @param {{ready:boolean,appearance:import('react').ReactNode}} props */
function HeaderActions({ready, appearance}) {
  return <><Badge>Local workspace</Badge>{appearance}
    <Button id="export" size="lg" disabled={!ready} onClick={shell.exportProgress}>Export progress</Button>
    <Button id="restore" size="lg" disabled={!ready} onClick={shell.restoreBackup}>Restore backup</Button>
  </>;
}
/** @type {{id:WorkspaceView,label:string}[]} */
const views = [{id:'hub',label:'Hub'},{id:'home',label:'Overview'},{id:'library',label:'Your curricula'},{id:'curriculum',label:'Learning path'},{id:'experiments',label:'Experiment journal'},{id:'guide',label:'Your approach'},{id:'sync',label:'Devices'}];
/** @param {{view:WorkspaceView,ready:boolean,navigating:boolean}} props */
function Navigation({view,ready,navigating}) {
  return <>{views.map(item => <Button key={item.id} variant="ghost" size="sm"
    className={'tab' + (view===item.id?' active':'')} data-view={item.id} aria-pressed={view===item.id}
    style={{fontSize:14,padding:'15px 0',borderRadius:0,border:0,borderBottom:'2px solid '+(view===item.id?'var(--ui-text)':'transparent')}}
    disabled={navigating || (!ready && !['sync','home','library','hub'].includes(item.id))}
    onClick={() => void shell.navigate(item.id)}>{item.label}</Button>)}</>;
}
async function resume() {
  if (!(await shell.navigate('curriculum'))) return;
  element(document,'#module-detail').scrollIntoView({behavior:'instant'});
  /** @type {HTMLElement | null} */(document.querySelector('#status'))?.focus({preventScroll:true});
}

/**
 * P03 keeps learning and connection views in one stable legacy region.
 * Only the reserved slots belong to React; the legacy controller never mutates
 * their children or attributes. Later screen steps replace regions independently.
 * @param {{onMounted:()=>void,appearance:import('react').ReactNode}} props
 */
function WorkspaceShell({onMounted,appearance}) {
  const host=useRef(/** @type {HTMLDivElement | null} */(null));
  const [slots,setSlots]=useState(/** @type {ShellSlots | null} */(null));
  const state=useSyncExternalStore(shell.subscribe,shell.getSnapshot);
  useLayoutEffect(()=>{
    const node=host.current;
    const template=document.querySelector('#legacy-workspace');
    if (!node || !(template instanceof HTMLTemplateElement)) throw Error('The local workspace template is unavailable.');
    const content=template.content.cloneNode(true);
    if (!(content instanceof DocumentFragment)) throw Error('The local workspace template could not load.');
    const mounts={actions:element(content,'#header-actions-slot'),navigation:element(content,'#navigation-slot'),resume:element(content,'#resume-slot'),progress:element(content,'#progress-slot'),connection:element(content,'#connection-slot'),home:element(content,'#home-view'),library:element(content,'#library-view'),hub:element(content,'#hub-view')};
    node.append(content);
    setSlots(mounts);
    return ()=>node.replaceChildren();
  },[]);
  useLayoutEffect(()=>{if(slots)onMounted();},[slots,onMounted]);
  return <><div id="hub-host" data-renderer="legacy" ref={host}/>{slots && <>
    {createPortal(<ConnectionScreen/>,slots.connection)}
    {createPortal(<HomeScreen/>,slots.home)}
    {createPortal(<LibraryScreen/>,slots.library)}
    {createPortal(<HubScreen/>,slots.hub)}
    {createPortal(<HeaderActions ready={state.ready} appearance={appearance}/>,slots.actions)}
    {createPortal(<Navigation view={state.view} ready={state.ready} navigating={state.navigating}/>,slots.navigation)}
    {createPortal(<Button variant="primary" size="lg" disabled={!state.ready||state.navigating} onClick={()=>void resume()}>Continue learning <span aria-hidden="true">↗</span></Button>,slots.resume)}
    {createPortal(state.progress && <Progress label={`${state.progress.done} of ${state.progress.total} modules complete`} value={state.progress.done} max={state.progress.total}/>,slots.progress)}
  </>}</>;
}
/** @param {{onMounted:()=>void}} props */
function Application({onMounted}) {
  const {theme}=useAppearance();
  return <ThemeProvider theme={theme}><WorkspaceShell onMounted={onMounted} appearance={<ThemeSwitcher/>}/></ThemeProvider>;
}

/** @param {{error:Error}} props */
function StartupFailure({error}) {
  return <main role="alert" style={{maxWidth:640,margin:'64px auto',padding:24,color:'var(--ui-text, #f5f5f5)',fontSize:16}}>
    <h1>Schola could not start</h1>
    <p>Reload to try again. If this keeps happening, share the error below when reporting the problem.</p>
    <p style={{overflowWrap:'anywhere'}}>{error.message}</p>
    <button type="button" style={{minHeight:44,minWidth:88,fontSize:16}} onClick={()=>window.location.reload()}>Reload</button>
  </main>;
}
/** @extends {Component<{children:import('react').ReactNode,onFailure:(error:Error)=>void},{error:Error|null}>} */
class ApplicationBoundary extends Component {
  state={error:/** @type {Error|null} */(null)};
  /** @param {Error} error */
  static getDerivedStateFromError(error) { return {error}; }
  /** @param {Error} error */
  componentDidCatch(error) { this.props.onFailure(error); }
  render() { return this.state.error ? <StartupFailure error={this.state.error}/> : this.props.children; }
}

// One root owns every React component, including startup recovery. Rejecting the
// mount promise also stops controller loading when the shell cannot mount.
/** @type {(reason:unknown)=>void} */
let rejectMount=()=>{};
let failed=false;
/** @param {unknown} reason */
function fail(reason) {
  const error=reason instanceof Error ? reason : new Error(String(reason));
  if (!failed) {
    failed=true;
    console.error('Schola startup failed:',error);
    root.render(<StartupFailure error={error}/>);
  }
  rejectMount(error);
}
const root=createRoot(element(document,'#app-root'),{onUncaughtError:fail});
try {
  await new Promise((resolve,reject)=>{
    rejectMount=reject;
    root.render(<ApplicationBoundary onFailure={fail}><Application onMounted={()=>resolve(undefined)}/></ApplicationBoundary>);
  });
  if (!failed) await import('./app.js');
  if (!failed) await import('./sync.js');
} catch (error) {
  fail(error);
}
