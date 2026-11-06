import {Button} from '@hannasage/projection-ui/core';
import {safeText} from './text.js';

/** @typedef {{id: string, text: string}} TutorOfferData */
const TEXT_LIMIT = 160;

/**
 * The tutor offer: one line of text and three buttons. It shows nothing when there is no offer or when offers are muted.
 * No server route sends offers yet, so the hub screen does not mount this with real data.
 * The text is plain text from outside. It is shown as a text node: no markup, no link, no markdown.
 * @param {{offer?: TutorOfferData | null, muted?: boolean, onAccept?: (id: string) => void, onDismiss?: (id: string) => void, onMute?: (id: string) => void}} props
 */
export function TutorOffer({offer, muted = false, onAccept, onDismiss, onMute}) {
  if (!offer || muted) return null;
  const {id} = offer;
  return <div className="hub-tutor" data-tutor-offer aria-label="Tutor offer" role="group">
    <span className="hub-tutor-tag">Tutor</span>
    <span className="hub-tutor-text" data-tutor-offer-text>{safeText(offer.text, {limit: TEXT_LIMIT})}</span>
    <span className="hub-tutor-actions">
      <Button type="button" variant="primary" size="sm" onClick={() => onAccept?.(id)}>Accept</Button>
      <Button type="button" size="sm" onClick={() => onDismiss?.(id)}>Dismiss</Button>
      <Button type="button" variant="ghost" size="sm" onClick={() => onMute?.(id)}>Mute</Button>
    </span>
  </div>;
}
