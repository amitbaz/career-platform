## Summary

<!-- What changed and why. Link the issue if one exists: Closes #123 -->

## Scope

<!-- Check every area this PR touches -->

- [ ] `apps/job-hunter`
- [ ] `apps/relay`
- [ ] `supabase` (migrations / schema)
- [ ] CI / workflows / tooling
- [ ] Docs only

## Testing

<!-- Commands you ran and their result. See AGENTS.md for the full command list. -->

```
pnpm relay:test        # if apps/relay changed
pnpm job-hunter:test    # if apps/job-hunter changed
```

## Notes for the reviewer

<!-- Anything non-obvious: deliberate scope cuts, follow-ups filed, migrations that need
     `supabase db push` before this is live, config/secrets that need to change outside git. -->
