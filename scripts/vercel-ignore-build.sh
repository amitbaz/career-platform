#!/usr/bin/env sh
# Vercel "Ignored Build Step" helper.
#
# Exit 0  -> skip this build.
# Exit 1  -> run this build.
#
# Called from a project's vercel.json `ignoreCommand`, which runs with the
# project's root directory as the working directory. Arguments are git
# pathspecs describing everything this project is built from. Paths outside the
# project's own directory are given with git's `:/` prefix, which resolves
# against the repository root.
#
# The bias throughout is to build. A build that runs unnecessarily costs a few
# minutes; a build wrongly skipped ships nothing and is much harder to notice.

# Never skip production. A skipped production build silently leaves the live
# deployment stale, which is the one outcome worth spending minutes to avoid.
if [ "${VERCEL_ENV:-}" = "production" ]; then
  exit 1
fi

# Without a previous SHA there is nothing to compare against — first deploy on a
# branch, or a rebuild triggered outside git. Build.
if [ -z "${VERCEL_GIT_PREVIOUS_SHA:-}" ]; then
  exit 1
fi

# Vercel checks out shallowly, so the previous SHA is not always present in the
# local object store. If we cannot read it, we cannot diff against it. Build.
if ! git cat-file -e "${VERCEL_GIT_PREVIOUS_SHA}" 2>/dev/null; then
  exit 1
fi

# Skip only when nothing this project is built from has changed.
if git diff --quiet "${VERCEL_GIT_PREVIOUS_SHA}" HEAD -- "$@"; then
  echo "No changes in $* since ${VERCEL_GIT_PREVIOUS_SHA}; skipping build."
  exit 0
fi

echo "Changes detected in $* since ${VERCEL_GIT_PREVIOUS_SHA}; building."
exit 1
