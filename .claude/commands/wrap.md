Wrap up the current session. Do the following steps in order:

## 1. Gather State

- Run `git log --oneline main@{upstream}..HEAD` to find unpushed commits
- Run `git diff --stat` and `git status` to find uncommitted changes
- Run `git log --oneline -20` to see recent commits from this session

## 2. Save Non-Obvious Takeaways to Memory

Review the session and identify anything a future session needs to know that ISN'T derivable from git log or reading the code. Examples:
- Cleanup tasks left behind (debug logging to remove, TODOs)
- Known data gaps or limitations discovered
- Performance concerns identified but not yet addressed
- Decisions made with non-obvious reasoning

Save each as a memory file (project type for active work items, feedback type for workflow lessons). Update MEMORY.md index. Do NOT append session logs to CLAUDE.md.

If nothing non-obvious was learned, skip this step — don't create memory files just for the sake of it.

## 3. Commit and Push

If there are uncommitted changes:
- Ask the user if they want to commit and push
- If yes, commit with message "Session wrap: [brief summary]" and push

If everything is already committed and pushed, just confirm that.

## Important

- Do NOT write session summaries or logs to CLAUDE.md — git log is the history
- Only save to memory what can't be derived from code or git
- Keep memory files focused and actionable
