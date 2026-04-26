#!/usr/bin/env bash
# Hook script for the project documentation system.
#
# Called from .claude/settings.local.json on SessionStart and PreCompact
# events. Outputs JSON whose `hookSpecificOutput.additionalContext` is
# injected into Claude's context, ensuring the AI is reminded of the
# documentation rules at session start and before compaction.
#
# Usage: bash .claude/hooks/doc-context-hook.sh {SessionStart|PreCompact}
#
# This script is intentionally local-only (.claude/ is gitignored). The
# canonical project docs (MEMORY.md, TODO.md, etc.) live at the project
# root and ARE committed; this hook just enforces that Claude reads and
# updates them.

set -euo pipefail
EVENT="${1:-}"

case "$EVENT" in
  SessionStart)
    CONTEXT='=== PROJECT DOCUMENTATION SYSTEM ===

This project uses a set of persistent Markdown documentation files as
long-term memory across sessions and compaction. They live at the project
root and are the source of truth for "where we are" — keep them current.

FILE PURPOSES (read MEMORY.md FIRST in any new session before doing
meaningful work):

  MEMORY.md
      Current project state, recent decisions made, in-flight workstreams,
      and immediate next steps. The single best document for picking up
      context fast. Update it after meaningful work and BEFORE compaction.

  TODO.md
      Prioritized backlog. Pending features, known bugs, technical debt.
      The top section is the priority queue — work down it in order. Mark
      items done as you finish them; add new items as you discover them.

  ARCHITECTURE.md
      System design: module responsibilities, data flow diagram,
      idempotency keys per stage, data shapes, cost profile, where the
      seams are. Update when pipeline structure changes (new module,
      changed data flow, new dedup key).

  TARGET_SITES.md
      Every external data source the pipeline touches: API endpoints, URL
      patterns, authentication, rate limits, hard-won discoveries. Update
      when you learn something new about an external system.

  AGENTS.md
      Roles, model choice, tool access, and permission matrix for every
      module (LLM agents and deterministic scrapers). Update when an
      agent prompt, model, structured-output schema, or permission
      changes.

  README.md
      Public-facing project overview. Update when the externally-visible
      shape of the project changes (run instructions, supported sources,
      etc.).

CONTINUOUS UPDATE RULES (apply as work happens — not at the end):

  - Finish meaningful work       -> update MEMORY.md
                                    (Where we are right now,
                                    Recent commits)
  - Complete a TODO item         -> mark done or remove it from TODO.md
  - Discover new work            -> add an entry under the right section
                                    of TODO.md (Pending features /
                                    Known bugs / Technical debt)
  - Change pipeline structure    -> update ARCHITECTURE.md
  - Learn something about an
    external system               -> update TARGET_SITES.md
  - Change agent config           -> update AGENTS.md
  - Before compaction             -> update MEMORY.md (the PreCompact
                                    hook will remind you)

Stale docs are worse than no docs. Treat these files with the same care
as code — review them at session start, update them as work happens.'
    ;;

  PreCompact)
    CONTEXT='COMPACTION REMINDER

Before this conversation is compacted, update the project documentation so
post-compaction context is not stale:

  1. MEMORY.md
     - Refresh "Where we are right now" with the current state.
     - Add this sessions key decisions to "Recent key decisions".
     - List any in-flight workstreams that are not yet finished under
       "Active workstream" or similar.
     - Add new commits to "Recent commits".

  2. TODO.md
     - Mark any TODO items that were completed this session.
     - Add any new TODOs that were discovered.

  3. ARCHITECTURE.md / TARGET_SITES.md / AGENTS.md
     - Update if any of pipeline structure, external systems, or agent
       configuration changed this session.

These docs are the bridge across compaction. If they are not current,
the next instance of you will be working with stale context.'
    ;;

  *)
    echo "doc-context-hook.sh: unknown event '$EVENT' (expected SessionStart or PreCompact)" >&2
    exit 1
    ;;
esac

# Use Python (already a project dependency) for JSON encoding rather than
# jq, which isnt installed by default on Windows. CONTEXT is passed via
# env var to avoid argv-escaping issues with the multi-line content.
export CONTEXT EVENT
python -c "
import json, os
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': os.environ['EVENT'],
        'additionalContext': os.environ['CONTEXT'],
    }
}))
"
