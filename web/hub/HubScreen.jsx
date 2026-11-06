import {useMemo, useSyncExternalStore} from 'react';
import {ThemeProvider, Alert, Accordion, Button, DataTable, Skeleton} from '@hannasage/projection-ui/core';
import {shell} from '../shell/bridge.js';
import {createHubService, HubServiceError} from '../services/hub.js';
import {benchTheme} from './theme.js';
import {isEmptyLog, logNotices, hintInfo, libraryLabel, plural, HINT_LADDER} from './model.js';
import {safeText} from './text.js';
import {useHub} from './useHub.js';
import {Panel} from './Panel.jsx';
import {Readout} from './Readout.jsx';
import {Heatmap} from './Heatmap.jsx';
import {TaskMeter, CheckpointGrid, SkillRow} from './Meters.jsx';
import {TutorOffer} from './TutorOffer.jsx';
import './hub.css';

/** The hub is dark only, with a 4 px radius. The rest of the app keeps its own theme. */
const THEME_STYLE = /** @type {import('react').CSSProperties} */({'--ui-radius-sm': '2px', '--ui-radius-md': '3px', '--ui-radius-lg': '4px'});

/** @typedef {import('./TutorOffer.jsx').TutorOfferData} TutorOfferData */
/** @typedef {{offer?: TutorOfferData | null, muted?: boolean, onAccept?: (id: string) => void, onDismiss?: (id: string) => void, onMute?: (id: string) => void}} TutorProps */

const HOW_ITEMS = [
  'A skill is a label on a check. The author of a module adds the label.',
  'For each skill, Schola takes the latest 5 results, in the order of your event log.',
  'Only a result of passed counts as a pass. Failed and error results count as tried and not passed.',
  'Skipped and self-reported results are not evidence. They do not take a place in the 5.',
  'The estimate is passed over tried. With fewer than 5 results it rests on fewer results.',
  'It is a guide to your own record. It is not a grade, and it is compared with nothing.',
];

function HowComputed() {
  return <div className="hub-how" data-how>
    <ul>{HOW_ITEMS.map(item => <li key={item}>{item}</li>)}</ul>
    <p>The full rules are in docs/hub-data.md, under Skills.</p>
  </div>;
}

/** @param {{focus: HubFocus | null, libraryCount: number, onContinue?: (focus: HubFocus) => void, tutor?: TutorProps}} props */
function ContinuePanel({focus, libraryCount, onContinue, tutor}) {
  return <Panel id="hub-continue" title="Continue" footer={<TutorOffer {...tutor}/>}>
    {focus ? <>
      <h3 className="hub-focus-title" data-focus-title>{safeText(focus.title, {limit: 120})}</h3>
      <dl className="hub-meta">
        <div><dt>module</dt><dd>{safeText(focus.moduleId, {limit: 60})} v{safeText(focus.version, {limit: 30})}</dd></div>
        <div><dt>tasks done</dt><dd>{focus.tasksDone}/{focus.tasksTotal}</dd></div>
        {focus.tasksSkipped > 0 && <div><dt>tasks skipped</dt><dd>{focus.tasksSkipped}</dd></div>}
      </dl>
      <div className="hub-next">
        <div>
          {focus.nextTask ? <>
            <p className="hub-next-label">next task · {safeText(focus.nextTask.taskId, {limit: 60})}</p>
            <p className="hub-next-text" data-next-task>{safeText(focus.nextTask.text, {limit: 600, multiline: true})}</p>
          </> : <p className="hub-next-text" data-next-task>{focus.tasksTotal === 0 ? 'This module has no tasks.' : 'Every task in this module has a result.'}</p>}
          <TaskMeter done={focus.tasksDone} skipped={focus.tasksSkipped} total={focus.tasksTotal}/>
        </div>
        {onContinue && <Button variant="primary" size="lg" onClick={() => onContinue(focus)}>Continue</Button>}
      </div>
    </> : <p className="hub-empty" data-empty="focus">{libraryCount === 0
      ? 'No lesson modules yet. Import a lesson module and it shows here.'
      : 'Every module in your library is done. Nothing is open.'}</p>}
  </Panel>;
}

/** @param {{participation: HubParticipation}} props */
function ParticipationPanel({participation}) {
  const {totals} = participation;
  return <Panel id="hub-participation" title="Participation" span={7}>
    <Readout items={[
      {value: totals.sessions, label: totals.sessions === 1 ? 'session' : 'sessions'},
      {value: totals.activeDays, label: totals.activeDays === 1 ? 'active day' : 'active days'},
      {value: totals.days, label: `days in ${participation.windowWeeks} weeks`},
    ]}/>
    <Heatmap participation={participation}/>
    <p className="hub-note">A session is one sitting. An active day is a day with at least one session. These are plain counts.</p>
  </Panel>;
}

/** @param {{checkpoints: HubCheckpoints}} props */
function CheckpointsPanel({checkpoints}) {
  const {done, total} = checkpoints;
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

/** @param {{skills: HubSkill[]}} props */
function SkillsPanel({skills}) {
  return <Panel id="hub-skills" title="Skill estimate" span={5}>
    <Accordion items={[{value: 'how', title: 'How is this computed?', content: <HowComputed/>}]}/>
    {skills.length === 0
      ? <p className="hub-empty" data-empty="skills" style={{marginTop: 12}}>No skill estimates yet. Skill estimates need modules that tag their checks with skills. None of your modules do yet.</p>
      : <ul className="hub-skills" data-skills>{skills.map(skill => <SkillRow key={skill.skillId} skill={skill}/>)}</ul>}
  </Panel>;
}

/** @param {{checks: HubCheck[]}} props */
function ChecksPanel({checks}) {
  const rows = checks.map((check, index) => ({...check, key: 'check-' + index}));
  /** @type {import('@hannasage/projection-ui/core').Column<(typeof rows)[number]>[]} */
  const columns = [
    {key: 'check', header: 'Check', cell: row => <>
      <span className="hub-check-title">{safeText(row.title, {limit: 160})}</span>
      <span className="hub-check-id">{safeText(row.moduleId, {limit: 60})} · {safeText(row.checkId, {limit: 60})}</span>
      {row.open && <span className="hub-check-open">Not passed yet</span>}
    </>},
    {key: 'retries', header: <>Retries<span className="hub-sr"> before first pass</span></>, align: 'right', width: '4.5rem', cell: row => <span className="hub-num">{row.retriesBeforeFirstPass}{row.open ? ' so far' : ''}</span>},
    {key: 'hint', header: <>Hint<span className="hub-sr"> level reached</span></>, width: '7rem', cell: row => {
      const hint = hintInfo(row.hintLevelReached);
      return <span className="hub-num"><span className="hub-pips" aria-hidden="true">{HINT_LADDER.map((name, at) => <i key={name} data-on={at < hint.step ? '' : undefined}/>)}</span>{hint.label}</span>;
    }},
  ];
  return <Panel id="hub-checks" title="Tries and hints" span={7}>
    {checks.length === 0
      ? <p className="hub-empty" data-empty="checks">No check has a result yet.</p>
      : <div className="hub-checks" data-checks><DataTable columns={columns} data={rows} rowKey={row => row.key} aria-label="Recent checks, with retries before the first pass and the hint level reached"/></div>}
    <p className="hub-note">This is a record of how each check went, for you to read. Retries are the tries before the first pass. Hint is the highest hint level reached. Both are a normal part of learning.</p>
  </Panel>;
}

/** @param {{library: HubLibraryEntry[]}} props */
function Library({library}) {
  return <section className="hub-rail" aria-labelledby="hub-library" tabIndex={0} data-panel="hub-library">
    <h2 className="hub-rail-title" id="hub-library">Library</h2>
    {library.length === 0
      ? <p className="hub-empty" data-empty="library">No lesson modules yet.</p>
      : <ol className="hub-lib" data-library>
        {library.map((entry, index) => <li key={entry.moduleId + '@' + entry.version + '#' + index} data-status={entry.status}>
          <span className="hub-lib-no">{String(index + 1).padStart(2, '0')}</span>
          <span>
            <span className="hub-lib-title">{safeText(entry.title, {limit: 120})}</span>
            <span className="hub-lib-state">{libraryLabel(entry.status)} · {entry.tasksDone} of {plural(entry.tasksTotal, 'task')}</span>
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
  const known = error instanceof HubServiceError;
  const detail = known && error.detail ? safeText(error.detail, {limit: 200}) : '';
  return <div className="hub-state" data-hub-state="error">
    <Alert tone="danger" title="Schola cannot load your hub">
      <p>{known ? error.message : 'Something went wrong. Try again.'}</p>
      {detail && <p className="hub-note">The server said: {detail}</p>}
    </Alert>
    <div><Button variant="primary" onClick={onRetry}>Try again</Button></div>
  </div>;
}

/** @param {{hub: HubResponse, onContinue?: (focus: HubFocus) => void, tutor?: TutorProps}} props */
function Ready({hub, onContinue, tutor}) {
  const notices = logNotices(hub);
  const empty = isEmptyLog(hub);
  return <div className="hub-fade" data-hub-state={empty ? 'empty' : 'ready'}>
    <div className="hub-shell">
      <div className="hub-main">
        {(notices.length > 0 || empty) && <div className="hub-notices" data-notices>
          {empty && <Alert tone="info" title="No activity yet" data-notice="empty-log">
            Your event log is empty. When you work on a task, your sessions and results show here.
          </Alert>}
          {notices.map(notice => <Alert key={notice.id} tone={notice.tone} title={notice.title} data-notice={notice.id}>{notice.text}</Alert>)}
        </div>}
        <ContinuePanel focus={hub.focus} libraryCount={hub.library.length} onContinue={onContinue} tutor={tutor}/>
        <ParticipationPanel participation={hub.participation}/>
        <CheckpointsPanel checkpoints={hub.checkpoints}/>
        <SkillsPanel skills={hub.skills}/>
        <ChecksPanel checks={hub.checks}/>
      </div>
      <Library library={hub.library}/>
    </div>
  </div>;
}

/**
 * The hub screen. It loads the numbers each time the Hub view opens.
 * `tutor` and `onContinue` are optional: no server route sends tutor offers yet, and no lesson screen exists to continue into.
 * @param {{service?: {getHub: (options?: {signal?: AbortSignal}) => Promise<HubResponse>}, tutor?: TutorProps, onContinue?: (focus: HubFocus) => void}} props
 */
export function HubScreen({service, tutor, onContinue}) {
  const hubService = useMemo(() => service ?? createHubService(), [service]);
  const view = useSyncExternalStore(shell.subscribe, () => shell.getSnapshot().view);
  const {state, reload} = useHub(view === 'hub', hubService);
  return <ThemeProvider theme={benchTheme} className="hub" style={THEME_STYLE} as="div">
    <section aria-labelledby="hub-title">
    <div className="hub-top">
      <h1 id="hub-title">Hub</h1>
      {state.hub && <span className="hub-updated">updated {safeText(state.hub.generatedAt, {limit: 40})}</span>}
    </div>
    <div aria-busy={state.status === 'loading' || state.status === 'idle'}>
      {(state.status === 'loading' || state.status === 'idle') && <LoadingState/>}
      {state.status === 'error' && <ErrorState error={state.error} onRetry={reload}/>}
      {state.status === 'ready' && state.hub && <Ready hub={state.hub} onContinue={onContinue} tutor={tutor}/>}
    </div>
    </section>
  </ThemeProvider>;
}
