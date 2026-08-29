# 1. Record architecture decisions

**Status:** accepted

## Context

ValKit's design contains several choices that look eccentric until the reason is
known — pure-Python statistics, an injected clock, content addressing rather
than checksums. Without a record, each will be "corrected" by someone acting
reasonably on incomplete information, and in a regulated tool some of those
corrections would be defects.

## Decision

Record consequential architectural decisions as short documents in `docs/adr/`,
in the standard form: context, decision, consequences — including the
consequences that count against the decision.

## Consequences

A reader can find out why something is the way it is without reconstructing it
from the code. The cost is that the records go stale unless superseded ones are
marked as such; a superseded decision keeps its file and gains a status line.
