# sbs-stats-data

Fetches the SBS battlefield stats from sbs-group.army and stores them in [data/sbs.db](data/sbs.db), a SQLite database in this repo. On the Raspberry Pi, the script commits the database after each run and pushes it over SSH with the repo's deploy key.

Tables:

- `daily_stats`: overall stats per day and hour.
- `monthly_stats`: overall stats per month.
- `subdivision_monthly_stats`: monthly stats per subdivision.

```sh
uv run fetch_and_push.py
```

## Configuration

Settings come from environment variables or a `.env` file next to the script. Real environment variables take precedence. Copy [.env.example](.env.example) to `.env`; `.env` is git-ignored.

| Variable | Default | Meaning |
|---|---|---|
| `SBS_DB_PATH` | `data/sbs.db` | SQLite database. Only this file is committed. |
| `PUBLISH` | `0` | `1` commits `data/sbs.db` and pushes after each run. Set it **only on the Pi**, so that one machine writes the data. |
| `PUBLISH_SSH_KEY` | — | Private key file of the deploy key, for git over SSH. |
| `PUBLISH_GIT_NAME` / `PUBLISH_GIT_EMAIL` | `sbs-stats-data` / `sbs-stats-data@bot.invalid` | Author of the data commits. |

## Raspberry Pi setup

### 1. Deploy key: write access to this repo only

A deploy key is an SSH key that grants access to a single repository. No personal access token is involved, nothing expires, and you can revoke it per repository.

```sh
ssh-keygen -t ed25519 -N "" -C "sbs-stats-data pi" -f ~/.ssh/sbs_stats_data_deploy
cat ~/.ssh/sbs_stats_data_deploy.pub
```

On GitHub, open the `sbs-stats-data` repo and go to **Settings → Deploy keys → Add deploy key**. Paste the public key and tick **Allow write access**.

### 2. Clone over SSH

The Pi pushes to this repo, so it must clone over SSH, not https:

```sh
GIT_SSH_COMMAND="ssh -i ~/.ssh/sbs_stats_data_deploy -o IdentitiesOnly=yes" \
  git clone git@github.com:foosint/sbs-stats-data.git ~/sbs-stats-data
```

To let a plain `git pull` use the deploy key too, set it once in the clone:

```sh
git config core.sshCommand "ssh -i ~/.ssh/sbs_stats_data_deploy -o IdentitiesOnly=yes"
```

### 3. Configure

```sh
cp .env.example .env
```

Edit `.env`:

```
PUBLISH=1
PUBLISH_SSH_KEY=/home/pi/.ssh/sbs_stats_data_deploy
```

If the repo doesn't contain `data/sbs.db` yet, copy an existing database there first, for example `cp ~/sbs-fetcher/sbs.db data/sbs.db`. Otherwise the first run starts a new, empty one.

Run it once by hand with `uv run fetch_and_push.py`. The log should end with `Committed sbs.db` and `Pushed to origin`.

### 4. Cron

Open the crontab with `crontab -e`:

```cron
9,19,29,39,49,59 * * * * cd /home/pi/sbs-stats-data && /home/pi/.local/bin/uv run fetch_and_push.py >> cron.log 2>&1
```

Use the full path to `uv`, because cron has a minimal `PATH`.

## Working from another machine

You can change the code anywhere and push. Before each push, the Pi runs `pull --rebase --autostash`, so it picks up your commits on its next run.

- Keep `PUBLISH=0` on every other machine.
- Never commit `data/sbs.db` from another machine. Only the Pi writes it, so the rebase never conflicts.

## Keeping the data commits off your GitHub profile

- **The pusher is the deploy key.** A deploy key belongs to the repository, not to a GitHub account, so pushes made with it don't count as your activity.
- **The author is a bot identity.** Commits are authored as `sbs-stats-data <sbs-stats-data@bot.invalid>`. That address isn't linked to any GitHub account, so GitHub attributes the commits to no one.

Don't set `PUBLISH_GIT_EMAIL` to an address linked to your account, and don't push with your personal SSH key or token.
