# Repository Guidelines

## Project Structure & Module Organization

Relay is a Next.js, TypeScript, and Tailwind CSS interview-coach POC. Keep route UI in `src/app/`: `page.tsx` is the client shell, `layout.tsx` provides layout, and `api/*/route.ts` contains route handlers. Put server-side persistence and AI logic in `src/lib/` (`db.ts`, `coach.ts`, and shared `types.ts`). Static files belong in `public/`; product and design notes belong in `docs/`. Runtime SQLite data lives under `data/` and must not be committed.

## Build, Test, and Development Commands

- `npm install` installs dependencies.
- `npm run dev` starts the local Next.js development server.
- `npm run lint` runs ESLint with the Next.js core-web-vitals and TypeScript rules.
- `npm run build` creates a production build; use `npx next build --webpack` when verifying in this environment, as documented in the README.
- `npm run start` serves a completed production build.

## Coding Style & Naming Conventions

Use TypeScript with strict typing and the `@/*` import alias for `src/` modules. Follow the existing two-space indentation and semicolon style. Use PascalCase for React components and type names, camelCase for functions and values, and lowercase route directory names (for example, `src/app/api/profile/route.ts`). Keep browser-only logic in client components and use `server-only` for server modules.

## Testing Guidelines

Vitest is the test runner, configured in `vitest.config.ts` with the jsdom environment and the `@/*` alias. Run the suite with `npm test` (`npm run test:watch` while iterating), or a single file with `npx vitest run <path>`. Co-locate `*.test.ts(x)` files next to the code they cover; only files under `src/` matching those suffixes are collected. Follow red → green → refactor: write a failing test, make it pass minimally, then improve both implementation and test. Run `npm test` and `npm run lint` plus a production build before submission.

## Commits, Pull Requests, and Configuration

Use concise, imperative Conventional Commit-style subjects, such as `docs: add interview design` or `feat: save interview checkpoints`. PRs should describe user-visible behavior, list verification commands, link the relevant issue or design note, and include screenshots for UI changes. Keep secrets in `.env.local`; never commit `GEMINI_API_KEY` or the local SQLite database.

## Source Code Documentation

- Document public modules, exported functions and types, API routes, and complex domain models: state their purpose, inputs and outputs, side effects, failure behavior, and important invariants.
- Write comments for intent and trade-offs—especially decisions, constraints, edge cases, security, or performance rationale that code alone cannot convey. Prefer clearer code over comments that merely restate it.
- Keep documentation close to the code it describes and update or remove it in the same change when behavior changes.
- Use examples for non-obvious APIs or workflows when they make correct usage clearer; keep examples minimal, runnable in context, and aligned with the current interface.
- Do not leave stale, speculative, or redundant comments. Use actionable TODOs only when they include the reason and a tracked next step.
- Treat documentation as part of code review: verify it is accurate, necessary, and helpful to a future maintainer.

## UI & Animation Guidelines (Mobile-First)

1. **Hardware-Accelerated Animations Only**
   Strictly limit CSS transitions and animations to `transform` (translate, scale, rotate) and `opacity`. Never animate layout-triggering properties (`width`, `height`, `top`, `left`, `margin`, `box-shadow`).

2. **Native-Like Page Transitions**
   Do not allow instant view changes or hard screen blinks. Implement smooth cross-fades or shared-element transitions using the native **View Transitions API**, or handle exit animations during unmounts (e.g., `<AnimatePresence>` in Framer Motion or `<Transition>` in Vue).

3. **Touch and Gesture Optimization**
   Rely on native browser scroll engines where possible. Use CSS `scroll-snap-type` for carousels and feeds instead of JS-based sliders. For custom drag/swipe components, explicitly set `touch-action: none` or `pan-y` to prevent interference with native browser scrolling.

4. **Spring Physics Over Easing**
   Default to spring physics for interactive UI elements instead of linear or standard CSS `ease-in-out` curves. Ensure all gestural animations calculate velocity and mass so they remain fluid and dynamically interruptible.

<!-- BEGIN:nextjs-agent-rules -->

# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` (resolved from this file's directory; in monorepos the `next` package may not be visible from the repo root) before writing any code. Heed deprecation notices.

This block is written and re-added by `next dev` — verify at `node_modules/next/dist/server/lib/generate-agent-files.js`. Removing it from a diff only re-creates the uncommitted change; committing it with your work keeps the tree clean.

<!-- END:nextjs-agent-rules -->
