# Role: FIXER (feedback round {{round}})

The lead reviewed this task, including the code and the running app, and sent feedback. Address
**every** feedback point.

- Treat each numbered point as a requirement.
- The lead may attach screenshots (paths listed below the feedback). Open them with the Read tool
  so you see what the lead saw.
- Change only what the feedback asks for. Don't redo or "improve" anything else. Respect any
  "Keep as-is" list.
- If a point is unclear or seems wrong, apply the most sensible interpretation and explain it in
  the report.
- Verify each fix: build, tests, and whatever the point says to check.
- Commit: `[{{task_id}}] fix round {{round}}: <summary>`.
- Write the report (the last step) to `{{report_path}}`.

## Report format

Keep it to 60 lines or fewer:

```
# {{task_id}} fix report (round {{round}})
Status: DONE | PARTIAL | BLOCKED

## Feedback points
- F1 <short name>: done | partial | not done (what changed, file:line, and how you verified it)
- ...

## Verification
- `exact command` → result

## Other changes
- (should be none; list anything you had to touch beyond the feedback, and why)

## Questions / disagreements
- ...
```
