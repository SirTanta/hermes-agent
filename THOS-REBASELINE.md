# THOS Hermes KISS Rebaseline

THOS is the operating system. Atlas is the durable ticket system of record. HubSpot is not part of this path.
Discord is the intentionally linked work transcript. Hermes executes one bounded job only when an Atlas ticket requires it.
Kanban is hidden execution plumbing, never the normal ticket, QA, or closure surface.

## Flow

Atlas ticket -> linked Discord work thread -> one Hermes execution job -> evidence to same Atlas ticket -> QA -> Atlas closure

## Execution Safety

Raphael is the sole dispatcher. Misa is the controlled fallback only when Raphael cannot execute recovery.
The live dispatch allowlist is raphael,misa; no other profile may receive normal execution work from the dispatcher.
The active gateway ceiling is five: Deed, Raphael, Misa, Sakuya, and Lexi. Stopped profiles remain installed and are not deleted.

## Rollback

The live dispatch policy has a timestamped backup in Raphael systemd drop-in directory. Source changes remain on this protected branch and are reversible with git revert HEAD. No automatic deploy or merge is part of this change.
