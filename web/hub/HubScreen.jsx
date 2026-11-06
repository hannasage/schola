import {useEffect, useMemo, useRef, useSyncExternalStore} from 'react';
import {ThemeProvider, Alert, Accordion, Button, DataTable, Skeleton} from '@hannasage/projection-ui/core';
import {shell} from '../shell/bridge.js';
import {createHubService, errorFacts} from '../services/hub.js';
import {benchTheme} from './theme.js';
import {isEmptyLog, eventsUnavailable, logNotices, hintInfo, libraryLine, plural, updatedLabel, UNAVAILABLE, HINT_LADDER} from './model.js';
import {displayText} from './text.js';
import {useHub} from './useHub.js';
import {Panel} from './Panel.jsx';
import {Readout} from './Readout.jsx';
import {Heatmap} from './Heatmap.jsx';
import {TaskMeter, CheckpointGrid, SkillRow} from './Meters.jsx';
import {TutorOffer} from './TutorOffer.jsx';
import {HubBoundary} from './HubBoundary.jsx';
import './hub.css';

/** The hub is dark only, with a 4 px radius. The rest of the app keeps its own theme. */
const THEME_STYLE = /** @type {import('react').CSSProperties} */({'--ui-radius-sm': '2px', '--ui-radius-md': '3px', '--ui-radius-lg': '4px'});

/** @typedef {import('./TutorOffer.jsx').TutorOfferData} TutorOfferData */
/** @typedef {{offer?: TutorOfferData | null, muted?: boolean, onAccept?: (id: string) => void, onDismiss?: (id: string) => void, onMute?: (id: string) => void}} TutorProps */

const HOW_ITEMS = [
  'A skill is a label on a check. The author of a module adds the label.',
  'For each skill, Schola takes the latest 5 results, in the order of your event log.',
  'Only a result of passed counts as a pass. Failed and error results count as tried and not passed.',
  'A skipped result is not evidence. It does not take a place in the 5.',
  'A self-reported check counts as passed for the check. It does not count as skill evidence.',
  'The estimate is passed over tried. With fewer than 5 results it rests on fewer results.',
  'It is a guide to your own record. It is not a grade, and it is compared with nothing.',
];

function HowComputed() {
  return <div className="hub-how" data-how>
    <ul>{HOW_ITEMS.map(item => <li key={item}>{item}</li>)}</ul>
  </div>;
}

/** The line that stands in for numbers that come from events when the log cannot be read. @param {{name: string}} props */
function Unavailable({name}) {
  return <p className="hub-empty" data-unavailable={name}>{UNAVAILABLE}</p>;
}

/** @param {{focus: HubFocus | null, libraryCount: number, unavailable: boolean, onContinue?: (focus: HubFocus) => void, tutor?: TutorProps}} props */
function ContinuePanel({focus, libraryCount, unavailable, onContinue, tutor}) {
  return <Panel id="hub-continue" title="Continue" footer={<TutorOffer {...tutor}/>}>
    {focus ? <>
      <h3 className="hub-focus-title" data-focus-title>{displayText(focus.title, {limit: 120})}</h3>
      <dl className="hub-meta">
        <div><dt>module</dt><dd>{displayText(focus.moduleId, {limit: 60})} v{displayText(focus.version, {limit: 30})}</dd></div>
        {unavailable
          ? <div><dt>tasks</dt><dd>{focus.tasksTotal}</dd></div>
          : <div><dt>tasks done</dt><dd>{focus.tasksDone}/{focus.tasksTotal}</dd></div>}
        {!unavailable && focus.tasksSkipped > 0 && <div><dt>tasks skipped</dt><dd>{focus.tasksSkipped}</dd></div>}
      </dl>
      {unavailable ? <div className="hub-next"><Unavailable name="continue"/></div> : <div className="hub-next">
        <div>
          {focus.nextTask ? <>
            <p className="hub-next-label">next task · {displayText(focus.nextTask.taskId, {limit: 60})}</p>
            <p className="hub-next-text" data-next-task>{displayText(focus.nextTask.text, {limit: 600, multiline: true})}</p>
          </> : <p className="hub-next-text" data-next-task>{focus.tasksTotal === 0 ? 'This module has no tasks.' : 'Every task in this module has a result.'}</p>}
          <TaskMeter done={focus.tasksDone} skipped={focus.tasksSkipped} total={focus.tasksTotal}/>
        </div>
        {onContinue && <Button variant="primary" size="lg" onClick={() => onContinue(focus)}>Continue</Button>}
      </div>}
    </> : <p className="hub-empty" data-empty="focus">{libraryCount === 0
      ? 'No lesson modules yet. Import a lesson module and it shows here.'
      : 'Every module in your library is done. Nothing is open.'}</p>}
  </Panel>;
}

/** @param {{participation: HubParticipation, unavailable: boolean, offsetFallback: boolean}} props */
function ParticipationPanel({participation, unavailable, offsetFallback}) {
  const {totals} = participation;
  if (unavailable) return <Panel id="hub-participation" title="Participation" span={7}><Unavailable name="participation"/></Panel>;
  return <Panel id="hub-participation" title="Participation" span={7}>
    <Readout items={[
      {value: totals.sessions, label: totals.sessions === 1 ? 'session' : 'sessions'},
      {value: totals.activeDays, label: totals.activeDays === 1 ? 'active day' : 'active days'},
      {value: totals.days, label: 'days counted so far'},
    ]}/>
    <Heatmap participation={participation}/>
    <p className="hub-note">A session is one sitting. An active day is a day with at least one session. These are plain counts.</p>
    {offsetFallback && <p className="hub-note" data-offset-note>Days use your current clock offset, so a day near a clock change can be off.</p>}
  </Panel>;
}

/** @param {{checkpoints: HubCheckpoints, unavailable: boolean}} props */
function CheckpointsPanel({checkpoints, unavailable}) {
  const {done, total} = checkpoints;
  if (unavailable) return <Panel id="hub-checkpoints" title="Checkpoints" span={5}>
    <Unavailable name="checkpoints"/>
    {total > 0 && <p className="hub-note">{plural(total, 'checkpoint')} in all.</p>}
  </Panel>;
  return <Panel id="hub-checkpoints" title="Checkpoints" span={5}>
    <p className="hub-cpn" data-checkpoint-count><b>{done}</b> of {total} done</p>
    {total === 0
      ? <p className="hub-empty" data-empty="checkpoints">No checkpoints yet. A checkpoint is a module you have finished.</p>
      : <>
        <CheckpointGrid done={done} total={total}/>
        <p className="hub-legend" aria-hidden="true">
          <span><i className="hub-swatch-done"/>done</span>
          <span><i className="hub-swatch-next"/>next</span>
          <span><i className="hub-swatch-ahead"/>not yet</span>
        </p>
        <p className="hub-note">A checkpoint is a module you have finished.</p>
      </>}
  </Panel>;
}

/** @param {{skills: HubSkill[], unavailable: boolean}} props */
function SkillsPanel({skills, unavailable}) {
  if (unavailable) return <Panel id="hub-skills" title="Skill estimate" span={5}><Unavailable name="skills"/></Panel>;
  return <Panel id="hub-skills" title="Skill estimate" span={5}>
    <Accordion items={[{value: 'how', title: 'How is this computed?', content: <HowComputed/>}]}/>
    {skills.length === 0
      ? <p className="hub-empty" data-empty="skills" style={{marginTop: 12}}>No skill estimates yet. Skill estimates need modules that tag their checks with skills. None of your modules do yet.</p>
      : <ul className="hub-skills" data-skills>{skills.map(skill => <SkillRow key={skill.skillId} skill={skill}/>)}</ul>}
  </Panel>;
}

/** @param {{checks: HubCheck[], unavailable: boolean}} props */
function ChecksPanel({checks, unavailable}) {
  const rows = checks.map((check, index) => ({...check, key: 'check-' + index}));
  /** @type {import('@hannasage/projection-ui/core').Column<(typeof rows)[number]>[]} */
  const columns = [
    {key: 'check', header: 'Check', cell: row => <>
      <span className="hub-check-title">{displayText(row.title, {limit: 160})}</span>
      <span className="hub-check-id">{displayText(row.moduleId, {limit: 60})} · {displayText(row.checkId, {limit: 60})}</span>
      {row.open && <span className="hub-check-open">Not passed yet</span>}
    </>},
    {key: 'retries', header: <>Retries<span className="hub-sr"> before first pass</span></>, align: 'right', width: '4.5rem', cell: row => <span className="hub-num">{row.retriesBeforeFirstPass}{row.open ? ' so far' : ''}</span>},
    {key: 'hint', header: <>Hint<span className="hub-sr"> level reached</span></>, width: '7rem', cell: row => {
      const hint = hintInfo(row.hintLevelReached);
      return <span className="hub-num"><span className="hub-pips" aria-hidden="true">{HINT_LADDER.map((name, at) => <i key={name} data-on={at < hint.step ? '' : undefined}/>)}</span>{hint.label}</span>;
    }},
  ];
  if (unavailable) return <Panel id="hub-checks" title="Tries and hints" span={7}><Unavailable name="checks"/></Panel>;
  return <Panel id="hub-checks" title="Tries and hints" span={7}>
    {checks.length === 0
      ? <p className="hub-empty" data-empty="checks">No check has a result yet.</p>
      : <div className="hub-checks" data-checks><DataTable columns={columns} data={rows} rowKey={row => row.key} aria-label="Recent checks, with retries before the first pass and the hint level reached"/></div>}
    <p className="hub-note">This is a record of how each check went, for you to read. Retries are the tries before the first pass. Hint is the highest hint level reached. Both are a normal part of learning.</p>
  </Panel>;
}

/** @param {{library: HubLibraryEntry[], unavailable: boolean}} props */
function Library({library, unavailable}) {
  return <section className="hub-rail" aria-labelledby="hub-library" tabIndex={0} data-panel="hub-library">
    <h2 className="hub-rail-title" id="hub-library">Library</h2>
    {unavailable && library.length > 0 && <Unavailable name="library"/>}
    {library.length === 0
      ? <p className="hub-empty" data-empty="library">No lesson modules yet.</p>
      : <ol className="hub-lib" data-library>
        {library.map((entry, index) => <li key={entry.moduleId + '@' + entry.version + '#' + index} data-status={unavailable ? undefined : entry.status}>
          <span className="hub-lib-no">{String(index + 1).padStart(2, '0')}</span>
          <span>
            <span className="hub-lib-title">{displayText(entry.title, {limit: 120})}</span>
            <span className="hub-lib-state">{unavailable ? plural(entry.tasksTotal, 'task') : libraryLine(entry)}</span>
          </span>
        </li>)}
      </ol>}
  </section>;
}

function LoadingState() {
  return <div className="hub-state" role="status" data-hub-state="loading">
    <p className="hub-mono">Loading your hub…</p>
    <div className="hub-skel" aria-hidden="true">
      <Skeleton height={28} width="55%"/><Skeleton height={14}/><Skeleton height={14} width="80%"/><Skeleton height={120}/>
    </div>
  </div>;
}

/** @param {{error: Error | null, onRetry: () => void}} props */
function ErrorState({error, onRetry}) {
  const known = /** @type {{message: string, code?: string, status?: number, serverCode?: string} | null} */(error && error.name === 'HubServiceError' ? error : null);
  const facts = known ? errorFacts(known) : '';
  return <div className="hub-state" data-hub-state="error">
    <Alert tone="danger" title="Schola cannot load your hub">
      <p>{known ? known.message : 'Something went wrong. Try again.'}</p>
      {facts && <p className="hub-note hub-mono" data-error-facts>{facts}</p>}
    </Alert>
    <div><Button variant="primary" data-hub-retry onClick={onRetry}>Try again</Button></div>
  </div>;
}

/** @param {{hub: HubResponse, offsetFallback: boolean, refreshing: boolean, onContinue?: (focus: HubFocus) => void, tutor?: TutorProps}} props */
function Ready({hub, offsetFallback, refreshing, onContinue, tutor}) {
  const notices = logNotices(hub);
  const empty = isEmptyLog(hub);
  const unavailable = eventsUnavailable(hub);
  return <div className={'hub-fade' + (refreshing ? ' hub-refreshing' : '')} data-hub-state={empty ? 'empty' : 'ready'} data-refreshing={refreshing || undefined}>
    <div className="hub-shell">
      <div className="hub-main">
        {(notices.length > 0 || empty) && <div className="hub-notices" data-notices>
          {empty && <Alert tone="info" title="No activity yet" data-notice="empty-log">
            Your event log is empty. When you work on a task, your sessions and results show here.
          </Alert>}
          {notices.map(notice => <Alert key={notice.id} tone={notice.tone} title={notice.title} data-notice={notice.id}>{notice.text}</Alert>)}
        </div>}
        <ContinuePanel focus={hub.focus} libraryCount={hub.library.length} unavailable={unavailable} onContinue={onContinue} tutor={tutor}/>
        <ParticipationPanel participation={hub.participation} unavailable={unavailable} offsetFallback={offsetFallback}/>
        <CheckpointsPanel checkpoints={hub.checkpoints} unavailable={unavailable}/>
        <SkillsPanel skills={hub.skills} unavailable={unavailable}/>
        <ChecksPanel checks={hub.checks} unavailable={unavailable}/>
      </div>
      <Library library={hub.library} unavailable={unavailable}/>
    </div>
  </div>;
}

/**
 * The hub screen. It loads the numbers each time the Hub view opens.
 * `tutor` and `onContinue` are optional: no server route sends tutor offers yet, and no lesson screen exists to continue into.
 * @param {{service?: import('../services/hub.js').HubService, tutor?: TutorProps, onContinue?: (focus: HubFocus) => void}} props
 */
export function HubScreen({service, tutor, onContinue}) {
  const hubService = useMemo(() => service ?? createHubService(), [service]);
  const view = useSyncExternalStore(shell.subscribe, () => shell.getSnapshot().view);
  const {state, reload} = useHub(view === 'hub', hubService);
  const title = useRef(/** @type {HTMLHeadingElement | null} */(null));
  const region = useRef(/** @type {HTMLDivElement | null} */(null));
  const refocus = useRef(false);
  const busy = state.status === 'loading' || state.status === 'idle' || state.refreshing;

  /** Try again removes the button that was pressed. Focus goes to the title at once, then to the new state. */
  const retry = () => {
    refocus.current = true;
    title.current?.focus({preventScroll: true});
    reload();
  };
  useEffect(() => {
    if (!refocus.current || busy) return;
    refocus.current = false;
    const target = state.status === 'error' ? region.current?.querySelector('button[data-hub-retry]') : title.current;
    if (target instanceof HTMLElement) target.focus({preventScroll: true});
  }, [busy, state.status]);

  const updated = state.hub ? updatedLabel(state.hub.generatedAt) || displayText(state.hub.generatedAt, {limit: 40}) : '';
  return <ThemeProvider theme={benchTheme} className="hub" style={THEME_STYLE} as="div">
    <section aria-labelledby="hub-title">
      <div className="hub-top">
        <h1 id="hub-title" tabIndex={-1} ref={title}>Hub</h1>
        {state.refreshing
          ? <span className="hub-updated" role="status" data-refreshing-note>Refreshing…</span>
          : state.hub && <span className="hub-updated" data-updated>{updated}</span>}
      </div>
      <div ref={region} aria-busy={busy}>
        <HubBoundary onRetry={reload} resetKey={state.hub}>
          {(state.status === 'loading' || state.status === 'idle') && <LoadingState/>}
          {state.status === 'error' && <ErrorState error={state.error} onRetry={retry}/>}
          {state.status === 'ready' && state.hub && <Ready hub={state.hub} offsetFallback={state.offsetFallback} refreshing={state.refreshing} onContinue={onContinue} tutor={tutor}/>}
        </HubBoundary>
      </div>
    </section>
  </ThemeProvider>;
}
