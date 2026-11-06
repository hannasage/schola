# Third-party software

Projection UI is licensed under MIT by Hanna Sage.
The vendored package is version 0.2.0-next.2 from the local Projection UI repository.
It is an alpha candidate and is not the published stable release.
Its license is included in the package archive.

The web application uses Projection UI ThemeProvider, Button, Badge, and Progress components.
Its remaining layout and fields use the same default theme tokens.
React, React DOM, Supabase JavaScript, and esbuild versions are pinned in `package-lock.json`.
The native application pins Supabase Swift 2.55.5 and records transitive versions in `Package.resolved`.

External learning resources and StatsBomb datasets are not bundled or relicensed by this project.
Follow their attribution and usage terms before distributing data or derivative work.

## Fonts

The Hub screen uses IBM Plex Sans and IBM Plex Mono.
They are copied from the npm packages below into `assets/fonts/`, Latin subset, WOFF2 format.
The local server serves them. The app adds no runtime dependency for them.

- IBM Plex Sans, weights 400, 500, and 600. Package `@fontsource/ibm-plex-sans` 5.3.0, published 2026-07-19. SIL Open Font License 1.1. Source: https://github.com/fontsource/font-files (directory `fonts/google/ibm-plex-sans`). License text: `assets/fonts/IBM-Plex-Sans-OFL-1.1.txt`.
- IBM Plex Mono, weights 400 and 500. Package `@fontsource/ibm-plex-mono` 5.3.0, published 2026-07-19. SIL Open Font License 1.1. Source: https://github.com/fontsource/font-files (directory `fonts/google/ibm-plex-mono`). License text: `assets/fonts/IBM-Plex-Mono-OFL-1.1.txt`.
