# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those roles to the actual label strings used in this repo's issue tracker.

| Label in mattpocock/skills | Label in our tracker | Meaning                                  |
| -------------------------- | -------------------- | ---------------------------------------- |
| `needs-triage`             | `needs-triage`       | Maintainer needs to evaluate this issue  |
| `needs-info`               | `needs-info`         | Waiting on reporter for more information |
| `ready-for-agent`          | `ready-for-agent`    | Fully specified, ready for an AFK agent  |
| `ready-for-human`          | `ready-for-human`    | Requires human implementation            |
| `wontfix`                  | `wontfix`            | Will not be actioned                     |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the corresponding label string from this table.

Edit the right-hand column to match whatever vocabulary you actually use.

## Repo note

These are the defaults, unchanged. This repo's existing issues (#29–#58, the
Week 3 / Week 4 roadmap) predate triage and are organised by roadmap week
rather than by these labels — so expect most open issues to carry no triage
label at all. That is not a backlog to sweep; `/triage` should only apply
these to newly filed bugs and requests.

Some work is inherently `ready-for-human` rather than `ready-for-agent` on
this project: anything requiring judgement against real hardware — most
notably tuning the 24 genre presets by ear, which needs the owner present and
the bulb powered.
