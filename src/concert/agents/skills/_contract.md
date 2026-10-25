## How to report

When you finish, write `.concert-run/result.json` as a JSON object:

```json
{"exit": "<one of the exits above>", "produced": ["paths"], "decisions": ["..."], "doubts": ["..."]}
```

Optional fields, all lists of strings:

- `lessons_applied`: the ids (like `L0012`) of the lessons you were given and followed.
- `lessons_proposed`: something this run taught you that the next one should know: a pitfall, a
  tooling quirk, a practice that worked. One short sentence each, with the "do this instead".
- `plan_deviations` (reviewers only): where the change departs from the approved spec.

Do not write anything else under `.concert-run/`. The service reads only that file and the files
you changed. If you cannot finish, still write the file with the most fitting exit.
