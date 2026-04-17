Wrap up the current session. Do the following steps in order:

## 1. Gather State

- Run `git log --oneline main@{upstream}..HEAD` to find unpushed commits
- Run `git diff --stat` and `git status` to find uncommitted changes
- Run `git log --oneline -20` to see recent commits from this session
- Check for any running tasks via TaskList
- Read the current CLAUDE.md to find the Session Log section (or note it doesn't exist yet)

## 2. Generate Summary

Write a session summary covering:

- **Date**: Today's date
- **Features built or changed**: List each feature/fix with a one-line description
- **Files modified**: Group by category (routes, templates, models, SDE, data, config)
- **Deploys**: How many deploys were done, any rollbacks or issues
- **Current state**: What's working, what's deployed, any known issues
- **Open decisions / TODO**: Anything the user mentioned wanting but hasn't been built yet
- **SDE status**: Whether a reimport is needed or was done, any new tables/columns added

## 3. Generate Resume Instructions

Write a "Resume prompt" — a suggested first message for the next session that gives full context to pick up where we left off. It should mention:
- What was just completed
- What the user was working on or testing
- Any next steps that were discussed

## 4. Append to CLAUDE.md

Append the summary under a `## Session Log` section at the bottom of CLAUDE.md. If the section already exists, append a new dated entry. Keep each entry concise (under 40 lines). Format:

```
## Session Log

### YYYY-MM-DD — [Brief title]
**Built**: feature1, feature2, ...
**Fixed**: bug1, bug2, ...
**Files**: list of key files touched
**State**: what's deployed and working
**Next**: suggested next steps
```

## 5. Commit and Push

If there are uncommitted changes:
- Ask the user if they want to commit and push
- If yes, commit with message "Session wrap: [brief summary]" and push

If everything is already committed and pushed, just confirm that.

## Important

- Be concise — the session log entry should be scannable, not a novel
- Focus on what a future session needs to know to resume effectively
- Don't repeat information already in CLAUDE.md's permanent sections
- The resume prompt should be copy-pasteable as a first message
