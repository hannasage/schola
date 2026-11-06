/**
 * A row of mono readouts: a number and what it counts.
 * @param {{items: {value: number | string, label: string}[]}} props
 */
export function Readout({items}) {
  return <ul className="hub-readout">
    {items.map(item => <li key={item.label}><b>{item.value}</b> {item.label}</li>)}
  </ul>;
}
