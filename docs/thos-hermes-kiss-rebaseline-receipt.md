# KISS Rebaseline Receipt

## Applied

- Live runtime branch: codex/thos-hermes-kiss-rebaseline-live.
- Dispatch allowlist is Raphael and Misa only, enforced by the highest-precedence Raphael systemd drop-in.
- Live gateway set is Deed, Raphael, Misa, Sakuya, and Lexi.
- Confirmed backup artifacts were quarantined outside the checkout; profiles and credentials were not removed.
- Atlas contract was verified against ATL-001005. HubSpot is absent from live gateway configuration.

## Verification

- Focused gateway capacity suite: 6 passed.
- Broader existing Kanban core suite: 172 passed, 9 failed. The failures are expected from the pre-existing technical-recovery change that returns crash/spawn/timeout failures to Misa triage instead of auto-blocking. This contract mismatch is recorded, not masked.
- Claude CLI is not installed on the host, so independent Claude review is deferred. A second Codex read-only review was used.

## Remaining Concrete Defects

- Raphael logs Discord 403 Missing Access for a stale destination. This is an access/configuration defect; no replacement Discord route was invented.
- A legacy Raphael session exceeded the provider input array limit (22624 > 16384). The graceful restart recovered it. Payload compaction needs a separate bounded fix.

## Rollback

- Restore the timestamped Raphael drop-in backup and restart Raphael.
- Restore quarantined backup artifacts from /home/mulagent/.hermes/quarantine.
- Revert this commit for source documentation.
# KISS Rebaseline Test Addendum

- Gateway capacity: 6 passed.
- Kanban dispatch lock: 5 passed.
- Direct technical recovery contract: passed. Two spawn failures route the same card to Misa triage with a technical_recovery event.
- Kanban core: 166 passed, 9 failed. Each failing assertion expects crashes, timeouts, or spawn failures to become blocked/gave_up. Live code intentionally returns those technical conditions to Misa triage, so the test contract must be updated in a separate bounded change.
- The two originally named profile-status/lifecycle test paths are absent from this live checkout.
- Claude CLI is absent, so Claude review is deferred.
