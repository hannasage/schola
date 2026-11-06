import {Card} from '@hannasage/projection-ui/core';

/**
 * A dense panel with a small mono title. The section is named by its title.
 * @param {{id: string, title: string, span?: 12 | 7 | 5, aside?: import('react').ReactNode, footer?: import('react').ReactNode, children: import('react').ReactNode}} props
 */
export function Panel({id, title, span = 12, aside, footer, children}) {
  return <section className={`hub-panel hub-s${span}`} aria-labelledby={id} data-panel={id}>
    <Card padding="none" material="solid" edgeLight={false} underglow={false}>
      <div className="hub-ph"><h2 id={id}>{title}</h2>{aside}</div>
      <div className="hub-pb">{children}</div>
      {footer}
    </Card>
  </section>;
}
