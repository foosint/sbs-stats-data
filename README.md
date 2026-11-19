# sbs-stats-data

Fetches the SBS battlefield stats from sbs-group.army into [data/sbs.db](data/sbs.db), a SQLite database in this repo:

- `daily_stats`: overall stats per day and hour.
- `monthly_stats`: overall stats per month.
- `subdivision_monthly_stats`: monthly stats per subdivision.

```sh
uv run fetch_and_push.py
```

A Raspberry Pi runs it every 10 minutes and commits and pushes the database. See [DEPLOY.md](DEPLOY.md) for the setup.

## Configuration

Environment variables, or a `.env` file next to the script (copy [.env.example](.env.example); real environment variables take precedence).

| Variable | Default | Meaning |
|---|---|---|
| `SBS_DB_PATH` | `data/sbs.db` | SQLite database. Only this file is committed. |
| `PUBLISH` | `0` | `1` commits and pushes the DB after each run. **Pi only.** |
| `PUBLISH_SSH_KEY` | — | Private key of the deploy key, for git over SSH. |
| `PUBLISH_GIT_NAME` / `PUBLISH_GIT_EMAIL` | `sbs-stats-data` / `sbs-stats-data@bot.invalid` | Author of the data commits. |
