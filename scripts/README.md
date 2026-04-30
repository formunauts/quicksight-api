# Miscellaneous Shell Helpers

The `scripts/` directory is not part of the main QuickSight CLI toolbox.

At the moment it contains one older operational helper:

## `delete_never_activated_users.sh`

- Purpose: delete inactive QuickSight users from the default namespace.
- Mutates: yes.
- Inputs: currently hard-coded `ACCOUNT_ID`.
- Usage:

```bash
bash scripts/delete_never_activated_users.sh
```

Because this script deletes users directly, review it before use. If it becomes part of a regular workflow later, it should be promoted into `code/` and rewritten with the same preview-first conventions as the rest of the toolbox.
