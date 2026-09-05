Task 4 report

- Added `ResultsFeedbackCards` in `src/app/results-feedback-cards.tsx` and wired the results view in `src/app/page.tsx` to use accessible disclosure buttons with `aria-expanded` and labelled detail regions.
- Expanded details now show the paired question and answer, populated dimension scores from the fixed nine-dimension list, strengths, missing points, better structure guidance, and improved answers while omitting legacy-empty sections safely.
- Added `src/app/page.test.tsx` to cover collapsed-by-default behavior, expansion toggling, detailed coaching content, and omission of empty legacy fields.
- Updated the Vitest setup for browser-style UI tests by enabling `jsdom`, adding a setup file, and installing the required Testing Library packages.

Verification

- `npm test -- src/app/page.test.tsx`
- `npm run lint -- src/app/page.tsx src/app/page.test.tsx src/app/results-feedback-cards.tsx`
- `npx next build --webpack`
- `git diff --check`

Notes

- The UI test harness changes were required because the repository previously only supported node-environment `.test.ts` files.
