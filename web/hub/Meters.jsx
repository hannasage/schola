import {meterSegments, checkpointCells, skillCells, skillPercent, plural} from './model.js';
import {safeText} from './text.js';

/** The strip of segments for the tasks of the focus module. The label says the same in words.
 * @param {{done: number, skipped: number, total: number}} props */
export function TaskMeter({done, skipped, total}) {
  const segments = meterSegments({done, skipped, total});
  const words = `${done} of ${plural(total, 'task')} done${skipped ? `, ${skipped} skipped` : ''}`;
  return <span className="hub-seg" role="img" aria-label={words} data-task-meter>
    {segments.map((state, index) => <i key={index} data-state={state}/>)}
  </span>;
}

/** The checkpoint grid, as a list. Each cell has words for a reader, so colour is never the only sign.
 * @param {{done: number, total: number}} props */
export function CheckpointGrid({done, total}) {
  const cells = checkpointCells(done, total);
  if (!cells.length) return null;
  const scaled = total > cells.length;
  return <ol className="hub-cp" aria-label={`${done} of ${plural(total, 'checkpoint')} done`} data-checkpoint-grid>
    {cells.map((state, index) => <li key={index} data-state={state}>
      <span className="hub-sr">{scaled ? `Part ${index + 1} of ${cells.length}` : `Checkpoint ${index + 1}`}: {state === 'done' ? 'done' : state === 'next' ? 'next' : 'not yet'}</span>
    </li>)}
  </ol>;
}

/** One skill: its label, a window of 5 cells, passed over tried, and a percent.
 * @param {{skill: HubSkill}} props */
export function SkillRow({skill}) {
  const cells = skillCells(skill);
  const pct = skillPercent(skill);
  const name = safeText(skill.skillId, {limit: 60});
  return <li className="hub-skill" data-skill>
    <span className="hub-skill-name">{name}</span>
    <span className="hub-cells" role="img" aria-label={`${name}: ${skill.passed} of ${skill.tried} passed. Based on the last ${skill.window} checks.`}>
      {cells.map((state, index) => <i key={index} data-state={state}/>)}
    </span>
    <span className="hub-skill-frac">{skill.passed}/{skill.tried}</span>
    <span className="hub-skill-pct">{pct === null ? 'n/a' : pct + '%'}</span>
  </li>;
}
