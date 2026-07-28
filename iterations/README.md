# Iterations Log

This folder exists so an agent picking this project up cold — including a
future instance of Claude Code — can see **what was actually tried**, not
just what shipped. Docs elsewhere (`README.md`, `SETUP.md`, `API.md`) describe
the finished, working state. This folder describes the *process*: attempts,
failures, root causes, and fixes, in the order they happened.

## When to add an entry

Add a new numbered entry whenever you do one of these:
- Build a genuinely new feature/subsystem (not a one-line bugfix).
- Hit a real failure while testing something against actual hardware/data
  (a device, a network, real audio, a real API) — not a typo you caught
  before running anything.
- Try an approach, abandon it, and pivot to a different one.

Small fixes that don't involve a real dead-end (e.g. "renamed a variable")
don't need an entry — `git log` already covers those. This log is for the
things git history *doesn't* explain on its own: why an approach was
abandoned, what a failure actually looked like, and what evidence proves the
fix worked.

## Structure

```
iterations/
  README.md                       <- this file
  001-<short-slug>/
    README.md                     <- the write-up (see template below)
    (optional: scratch scripts, logs, screenshots kept as evidence)
  002-<short-slug>/
    README.md
  ...
```

Numbers are sequential and never reused, even if an entry is later found to
be wrong or superseded — correct it in place or add a follow-up entry that
references it, don't renumber history.

## Entry template

Each `NNN-slug/README.md` should cover:

```markdown
# NNN — <Title>

## Goal
What this iteration was trying to accomplish, in one or two sentences.

## Approach
What was actually attempted. Concrete: commands run, libraries used,
endpoints hit — enough that someone could redo it.

## What happened
The honest result. If it worked first try, say so briefly. If not, keep
reading.

## Failures (if any)
For each dead end: what broke, the actual error/symptom observed (not a
guess), and the root cause once found. This is the part that saves the next
agent from repeating the same dead end.

## Fix
What changed to make it work. Reference the actual file/function.

## Verification
The real evidence it works now — a command that ran, an API response, a
value that moved, a screenshot. Not "should work" — what was actually
observed.
```

## Relationship to other docs

- `HANDOFF.md` (repo root) is the condensed, current-state summary — read
  that first for "what's true right now."
- This folder is the expanded, chronological version underneath it — read
  this when you need to know *why* something is built the way it is, or
  you're about to attempt something that might already have a known dead
  end recorded here.
- Existing project history prior to this folder's creation is preserved in
  `HANDOFF.md`'s "How local control was actually obtained" and "Bugs found
  and fixed" sections — that was written before this folder existed, so it
  isn't duplicated here retroactively.
