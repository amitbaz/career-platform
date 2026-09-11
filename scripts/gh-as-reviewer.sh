#!/usr/bin/env bash
# Kept so existing instructions keep working. The logic lives in scripts/gh-as.sh.
exec "$(dirname "$0")/gh-as.sh" reviewer "$@"
