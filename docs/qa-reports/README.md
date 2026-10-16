# QA reports

Two report-only QA passes run on 2026-10-15, before the fixes described in the main README.

- `qa-report-mixed-llm-agent-2026-10-15.md`: first pass (API + browser), including the code-review candidates.
- `run-20261015T113658Z/`: leftovers pass (real Gemini, OIDC, timeouts, races, accessibility, performance).
- `browser-sessions/`: screenshots from the earlier interactive browser tests.

Each finding was fixed afterwards and has a regression test in `tests/`. The reports describe the code as it was
when tested, so they show the bugs, not the current behaviour.

Redaction: a 6-character search string derived from a real API key appeared in a few evidence files (it was used to
check that the key was *absent* from logs). It has been replaced with `KEYPFX` in this copy, so the SHA-256 values
recorded for those captures no longer match the edited files.
