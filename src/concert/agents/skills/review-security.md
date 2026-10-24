# Security review

Review the change for security problems. Do not change any files.

Look for: injection of any kind, missing authorization or authentication checks, unsafe handling of
untrusted input, secrets in code or logs, unsafe deserialization, weak crypto, dependency risks,
and anything that widens what an attacker could reach. Pay particular attention to paths marked
security-sensitive.

Exits: `clean` or `changes_needed` (put each finding in `doubts`, with the file and why it matters).
