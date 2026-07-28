# Atlas Ticket Contract

Verified against live ticket ATL-001005 (e9bd0548-c57c-498d-8238-c6f93cafc6cb), which is closed with Hermes card t_194ba657.

Required Atlas-side facts for Hermes work:
- ticket UUID and number
- ticket state and assigned profile
- bounded desired result
- source authority
- allowed and prohibited action boundaries
- execution acceptance
- same-ticket evidence artifact or receipt
- QA result when QA is required

The ticket stores Hermes card ID/state, timestamps, result, artifact, receipt/proof links, QA fields, and atlas_ticket_links.
There is no native Discord-thread column in the verified ticket record. Discord linkage must be recorded deliberately as a ticket artifact or comment, never inferred from a Kanban card.
No Hermes card is authoritative by itself. A ticket may not claim delivery without independent proof.
