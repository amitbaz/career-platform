# Relay — AI Interview Coach

A personal, authenticated interview-practice POC for software-engineering candidates.

## What works now

- CV PDF upload or professional-summary paste → generated, editable professional profile
- Source-backed engineering evidence extraction with a meaningful-profile gate before grounded conversational interviews
- Supabase Postgres persistence with Google sign-in and user-scoped access
- Responsive Home, Profile, Practice, Progress, Interview, and Results screens
- Conversational interview sessions with a persisted five-question blueprint, saved messages, evidence-backed prompts, feedback, and follow-ups
- Hands-on React + TypeScript interview with persisted code checkpoints, interviewer prompts, and technical feedback
- Gradual competency updates after completed conversational and hands-on sessions
- Progress insights that separate no-evidence, first-session baseline, and multi-session trend states with recurring coaching themes
- Browser voice recording → Gemini transcription → editable interview answer
- Deterministic demo coach when no API key is configured
- Gemini API provider when `GEMINI_API_KEY` is configured
- Career Brain command center with application tracking, story bank, inspectable coach memory, and explainable recommended practice.
- Recommended practice currently uses deterministic explicit Career Brain/progress signals; automatic observation learning and richer adaptive prioritization arrive in the next release.
- The external job-hunter bot is still independent and has not yet been migrated to Supabase.

## Run locally

```bash
npm install
npm run dev
```

Open <http://localhost:3000>.

Current product boundary: the grounded conversational interview flow is implemented for software-engineering profiles only. Relay does not yet do live web research, market-intelligence caching, executable code evaluation, or a cloud IDE.

To use live Gemini responses, create `.env.local`:

```bash
GEMINI_API_KEY=your_key_here
# Optional; defaults to gemini-3.6-flash
GEMINI_MODEL=gemini-3.6-flash
```

Create the key in Google AI Studio. Gemini's free tier is rate-limited and Google states that free-tier content may be used to improve its products, so do not use it with sensitive career data. Keep `.env.local` private. Supabase stores profile, interview, and source-document data with user-scoped access policies.

## Supabase + Google setup

The hosted POC uses Supabase Postgres, Auth, and private Storage. For a local or Vercel deployment:

1. Create a free Supabase project. Install and authenticate the Supabase CLI, link the repository to the project, and run `supabase db push`. If the CLI is unavailable, paste and run every file in `supabase/migrations/` in filename order in the Supabase SQL Editor. Existing projects must also run `202608290002_complete_adaptive_interview_loop.sql`, `202608290003_richer_feedback.sql`, `202608310001_planned_practice_sessions.sql`, and, for this release, `202609040001_park_moves_off_target.sql` after pulling this update. `202609040001_park_moves_off_target.sql` replaces `record_conversation_turn` with a wider signature and drops the old one, so run the migration first and deploy the code immediately after. During the gap, calls made with the old parameter set find no matching function and every answer turn fails with a clean error ("Could not record your interview turn."); nothing is silently written wrong, and nothing is persisted either way.
2. Copy the Supabase project URL and publishable key from Project Settings → API into `.env.local`:

   ```bash
   NEXT_PUBLIC_SUPABASE_URL=https://your-project.supabase.co
   NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY=sb_publishable_your_key
   ```

3. In Google Cloud Console, create an OAuth client with application type **Web application**. Add `http://localhost:3000` and your Vercel production origin (for example, `https://your-app.vercel.app`) to **Authorized JavaScript origins**. In **Authorized redirect URIs**, add the Supabase Google provider callback URL shown in the Supabase Auth provider setup.
4. In Supabase, open **Auth → Providers → Google**, enable Google, and add the Google OAuth client ID and client secret.
5. In Supabase, open **Auth → URL Configuration**, set the **Site URL** to the deployment origin, and add these redirect URLs:

   ```text
   http://localhost:3000/auth/callback
   https://<vercel-domain>/auth/callback
   ```

6. In Vercel, add `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY`, `GEMINI_API_KEY`, and optional `GEMINI_MODEL` as project environment variables, then deploy from the connected repository.

Keep uploaded CV PDFs under 4 MB. Vercel Functions accept request payloads up to 4.5 MB, and the smaller application limit leaves room for multipart form overhead. Gemini errors shown by the app include a safe status and provider message; a `401`/`403` points to the API key or its restrictions, `404` points to `GEMINI_MODEL`, and `429` indicates rate limiting.

Supabase's free projects pause after one week of inactivity and do not include automatic backups. Resume inactive projects when needed and export/backup important data separately.

## Verify

```bash
npm run lint
npx next build --webpack
```

The Webpack verification build is used because Turbopack's CSS worker cannot bind a required local port in this environment.

## Next slices

- Live web research and a cached market-intelligence layer for current software-engineering interview expectations
- Executable candidate-code evaluation with preview, tests, console, and cloud-IDE-style workspace feedback
