# Web boundaries

`web.jsx` creates the only React root.
It owns the theme provider and the header, navigation, resume, and progress controls.
The appearance component stays mounted when learning state changes.

The HTML template in `index.html` holds the current learning and connection views.
The shell clones that template once into its legacy region.
React renders only into the reserved `data-renderer="react"` slots inside that region.
`app.js` can update the other elements, but cannot change those slots or their children.
New screens can replace a legacy region without replacing the shell.

`web/shell/bridge.js` carries shell state and named commands.
Navigation changes its selected view only after the controller accepts the transition.
A failed save keeps the current view and its working copy.
Selecting a view does not change curriculum focus or status.

`web/services/learning.js` handles local requests and structured server errors.
The caller supplies the account scope explicitly for each record operation.
The controller keeps the revision, account-change, and recovery checks around each awaited request.

`web/services/accounts.js` provides the learning operations that the connection view can call.
Its methods cover snapshots, scoped imports, scope changes, invalidation, and downloads.
The current `window.touchline` value is a compatibility facade for those methods.
The connection controller in `web/connection/` handles account discovery, sign-in, session checks, and remote transfers.
`sync.js` starts that controller after the learning bridge is ready.

Run `npm run test:browser` to exercise failed navigation and the retained learning workflows.
Run `npm run test:appearance` to check theme changes without remounting working forms.
The service and bridge unit tests run with `npm test`.

The P04 home and library screens are React portals in `web/screens/`.
They read explicit library focus and complete record metrics through the local service.
Opening a curriculum keeps the focus unchanged.
Status changes and focus changes use revision-checked writes, and failures leave the current learning record intact.
The lesson controller owns the lesson region and uses `web/screens/lesson.js` for its markup.
The connection screen is a React portal in the same shell.
Account invalidation clears private records from the home, library, and lesson views before loading local learning.
Journal, import, and appearance flows retain their existing owners.

## The Hub view

The Hub view is a React portal in `web/hub/`. It is the first item in the navigation, and it is the landing view: the shell starts on it and a reload returns to it.
`web/services/hub.js` calls `GET /api/hub` and checks the answer. `web/hub/model.js` holds the pure parts. `web/hub/text.js` makes lesson text safe to show.
The screen is dark only and uses its own Projection UI theme, so the appearance setting does not change it.
Its fonts are local files in `assets/fonts/`. `docs/hub-screen.md` has the states, the limits, and where each number comes from.
A crash while it draws is caught by `HubBoundary`, so the rest of the app keeps working.
`TutorOffer` is built and tested alone. No route sends offers yet, so the screen does not mount it with data.
Run `npm run test:hub` to run only the Hub browser checks.

## Journal and file review

The journal keeps one unfinished experiment per curriculum. Hypotheses, results, and evidence links use the portable progress fields. Links in results open in a new tab. Switching to another experiment requires confirmation before replacing an unfinished draft.

Import review validates the file without writing records. A new curriculum defaults to saved for later. It cannot replace an existing curriculum with the same identity. Restore backup replaces only the selected record after review, preserves its local identity, and rejects a stale saved revision. Legacy backups retain the current lesson selection.

When a damaged local record blocks startup, the recovery panel offers a validated retained backup for that record. The recovery request checks the current damaged bytes, backup contents, and recovery history. It reserves a new revision before writing the restored record. Repeated recovery and retries cannot reuse an older recovery revision. It preserves the damaged bytes in a separate file and leaves the retained backup intact. Reload other open Schola tabs after recovery. Cloud backups are excluded from this local recovery route.

To roll back these screens, retain all record and backup files. The earlier client can read the unchanged record format and the experiment evidence stored in result text.

## Explicit publications

`web/publications/` publishes selected records independently through the version 1 publication RPC. Each local record keeps a binding for its hub, backend origin, account, and curriculum. A pending request is saved before transmission; retries reuse its UUID and original payload even if local work changes. Missing browser storage blocks new writes. Damaged publication recovery stays untouched and disables new writes for that record.

A download first validates the complete public snapshot and shows the local comparison and export action. The commit rereads the cloud snapshot, then uses the existing atomic local import endpoint with the reviewed local revision. Replacement preserves origin, draft, selected lesson, status, and unrelated records. A removed draft or selected module blocks replacement. Account changes invalidate the preview.

Library focus has its own review and revision. Publishing it does not publish course contents. Course status travels only with the selected curriculum publication. Editing stays local until an explicit publication.

To roll back publication, remove its entry points and retain the browser recovery keys, local files, and existing cloud snapshots. No migration or deletion is required.
