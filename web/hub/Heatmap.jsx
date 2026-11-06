import {heatRows, WEEKDAY_NAMES} from './model.js';

const FULL_DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

/**
 * Sessions by day, one row for each week. It is a real table: every cell holds its count as text.
 * @param {{participation: HubParticipation}} props
 */
export function Heatmap({participation}) {
  const rows = heatRows(participation);
  return <>
    <table className="hub-heat" data-heat>
      <caption className="hub-sr">Sessions on each day of the last {participation.windowWeeks} weeks. One row for each week, starting on the Monday.</caption>
      <thead>
        <tr>
          <th scope="col"><span className="hub-sr">Week starting</span></th>
          {WEEKDAY_NAMES.map((name, index) => <th key={name} scope="col"><span aria-hidden="true">{name}</span><span className="hub-sr">{FULL_DAYS[index]}</span></th>)}
          <th scope="col" className="hub-sum"><span aria-hidden="true">Ses</span><span className="hub-sr">Sessions in the week</span></th>
          <th scope="col" className="hub-sum"><span aria-hidden="true">Days</span><span className="hub-sr">Active days in the week</span></th>
        </tr>
      </thead>
      <tbody>
        {rows.map(row => <tr key={row.start}>
          <th scope="row">{row.start.slice(5)}<span className="hub-sr"> (week starting {row.start})</span></th>
          {row.cells.map(cell => <td key={cell.date} data-level={cell.level} data-count={cell.count ?? ''}>
            {cell.count === null ? null : <span aria-hidden="true">{cell.count}</span>}
            <span className="hub-sr">{cell.text}</span>
          </td>)}
          <td className="hub-sum">{row.sessions}</td>
          <td className="hub-sum">{row.activeDays}</td>
        </tr>)}
      </tbody>
    </table>
    <p className="hub-legend" aria-hidden="true">
      <span><i className="hub-swatch-none"/>0</span>
      <span><i className="hub-swatch-one"/>1 session</span>
      <span><i className="hub-swatch-many"/>2 or more</span>
      <span><i className="hub-swatch-future"/>not yet</span>
      <span>Ses = sessions. Days = active days.</span>
    </p>
  </>;
}
