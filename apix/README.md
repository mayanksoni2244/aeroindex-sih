# AeroIndex (APIx) — package README

The project README lives at the repository root:

**→ [`../README.md`](../README.md)**

That file is the single source of truth for the file map, the live-vs-simulated
split, the test counts, run instructions and configuration. This file used to be
a second, independently-edited copy of it, and the two had drifted — it still
listed Yatra and MakeMyTrip adapters that no longer exist, quoted a test count
of 197 against a current 266, and described a Chhath Puja festival window that
`apix/config/festivals.yaml` does not define.

Two documents stating different values for the same claim is the one failure
mode this project treats as disqualifying, so the duplicate was removed rather
than updated twice. Everything is in the root README.
