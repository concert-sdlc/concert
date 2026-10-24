## How to report

When you finish, write `.concert-run/result.json` as a JSON object:

```json
{"exit": "<one of the exits above>", "produced": ["paths"], "decisions": ["..."], "doubts": ["..."]}
```

Do not write anything else under `.concert-run/`. The service reads only that file and the files
you changed. If you cannot finish, still write the file with the most fitting exit.
