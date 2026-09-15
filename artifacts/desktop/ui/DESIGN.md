# VibApp shared GUI

Brand line: **Find it. Vibe it.** — **发现所需，创造所想。**
Supporting copy: “Find one. Or create your own.” Web wordmark: `VibApp.ai`.

This is a working launcher, not a marketing landing page. Start with the request
input. Keep navigation stable: Discover, Store (Library on desktop), Activity, Settings. A single
language selector sits at the top right, including on narrow screens.

Use the bundled `artifacts/cloud-agent/skills/apple-design` reference: cool
`#f5f5f7`, white surfaces, system typography, one blue accent, named radius/shadow
tokens, thin dividers. Honor system dark mode with neutral grays. Use translucent
material only on overlapping navigation. No fake live dots or decorative stats.

Give the user's content and primary action priority. Keep headings short and
explanations to one sentence; technical metadata belongs in disclosure panels.
Settings form one grouped surface. Keep real warnings and unavailable states
visible; do not imply a service is connected simply because the GUI loaded.

Locale: an explicit device preference wins; otherwise the browser's primary
Chinese language maps to Simplified Chinese, and every other language to English.
Switching language preserves unsent text, form values and consent checkboxes in
memory, never in logs or persistent credential storage. User-authored content is
not translated. Localization must preserve labels, focus and 44px control targets.

Desktop and website consume the same `app.js` and `styles.css`. Change these
sources, run the production GUI sync, then prepare the Sites export. Do not fork
the GUI into a separate React/HTML implementation. Generated Rust apps follow
the design guidance via their immutable task inputs; the host still owns pixels.

Store browsing follows the macOS App Store reference: a quiet collection/search
sidebar, real-app spotlights and icon-led rows with Get/Open pills. No fake ratings,
editor picks, download counts or catalog fillers. Only published apps appear;
browser activation still requires a verified launch binding. Details preserve
permission warnings, source/release links and native-client handoff. Search and
collection selections survive details/back and language changes. Small screens
move the sidebar above the results without duplicating navigation or content.

Download opens a separate modal with OS/chip selection, a verified direct Release
asset link and release notes. Infer OS but never infer ARM from “MacIntel” or the
Safari UA. Unknown chips require a choice; unpublished targets remain unavailable.
Accept keyboard dismissal/focus restoration, reduced motion, dark mode and 44px
targets. Keep the explicit published-asset metadata in CLIENT_RELEASE aligned with
future client releases; never turn platform roadmap entries into download links.
