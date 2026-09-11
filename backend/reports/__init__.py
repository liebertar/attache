"""Read-only views over the ledger. Nothing in this package judges, locks, or executes.

The judging modules live one level up, in backend, and are held to "never read who
drew the route" (tests/test_drafter RuntimeNeverReadsTheDrafterTest). A report is the
opposite kind of code: it reads the ledger back to people, provenance included, and changes
nothing. Keeping it in its own package keeps that grep honest.
"""
