# clinician-audit

A private tool that checks virtual-care providers' activity **minute by minute** to spot possible time fraud:
a provider is scheduled and paid, patients are waiting, and the provider is not seeing patients.

* **No AI or language models are used when it runs.** Every flag is plain arithmetic on times, so any result can be explained and re-checked.
* **Only pseudonymous IDs.** It never asks for patient names or details. Anything in your Metabase report that is *not* listed in `config/column_map.yaml` is thrown away the moment it is downloaded.
* **Everything stays on your computer** in a local file (`data/clinician_audit.duckdb`). That folder is excluded from Git.

## What it flags (version 1)

| Flag | In plain English |
|---|---|
| Gap between consults | More than 20 minutes (NP/MD) or 15 minutes (CM) between two consults *inside a scheduled shift*. Shows how many patients were waiting in intake/triaged during the gap. |
| Empty hours with patients waiting | An hour or more at the start/end of a shift (or a whole shift) with no consults, while patients were waiting. |
| No video when video requested | Patient asked for a video call but none happened (NP/MD only). Needs two extra columns, see `config/column_map.yaml` (the two commented lines under `encounters`). |

Gaps inside **on-call** and **training** shifts are hidden; **admin** shifts are shown but labelled (change this in `config/settings.yaml` under `shift_type_policy`). Shift types come from Humanity.
More flags (short/long video calls, very long or short consults, missing/late notes, and so on) are planned and can be added as separate functions; see the top of `src/flags.py`.

### Care managers (CM): read this
There is no per-message data for CMs yet. The tool therefore does **not** treat "joined" to "last checked in" as working time. It uses `secs_of_participants_activity` as the engaged time and marks every CM result **low confidence**, with a warning on screen. Treat CM flags as leads to check. If you later get a message-level table, add it in `config/column_map.yaml` (there is a commented example called `cm_messages`) and the tool will use it automatically.

## 1. Install (one time)

You need Python 3.11 or newer.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Put your keys in `.env`

1. Copy the template: `cp .env.example .env` (Windows: `copy .env.example .env`).
2. Open `.env` in a text editor and fill in the blanks after the `=` sign, with no quotes:

| Setting | What it is |
|---|---|
| `METABASE_URL` | Your Metabase address, e.g. `https://metabase.example.com` |
| `METABASE_API_KEY` | An API key created in Metabase (Admin > Settings > Authentication > API keys) |
| `METABASE_CARD_ID` | The number in the master report's URL, e.g. `.../question/1234-...` means `1234` |
| `HUMANITY_API_KEY` | Your Humanity key (for shift types) |
| `SLACK_ENABLED` / `CHR_ENABLED` | Leave `false` unless you are using those sources |
| `SLACK_AUDIT_TOKEN`, `SLACK_BOT_TOKEN` | Only if Slack is enabled |

`.env` is in `.gitignore` so it is never committed. **Never paste a key into chat, email, or a file other than `.env`.** The tool never prints keys.

## 3. Check the column names (the one setup chore)

Open `config/column_map.yaml`. On each line the **left side** is the column name as it appears in *your* Metabase report and the **right side** is the tool's internal name. Fix the left side if your names differ. Never change the right side. Lines marked `CONFIRM` are best guesses that were not in the original spec. If a column does not exist, delete the line; the app will show a warning rather than crash.

Your Metabase question must accept two date parameters named `start_date` and `end_date` (rename them in `config/settings.yaml` under `metabase:` if yours differ). The tool asks for one day at a time.

## 4. Get the data, then look at it

```bash
python -m src.pull_metabase --start 2026-03-01 --end 2026-03-07
streamlit run app.py
```

Your browser opens the dashboard. In the left sidebar type a provider ID and pick the dates. You get four tabs:

1. **Timeline**: shifts, consults, Slack/CHR activity, and flagged gaps in red.
2. **Flags**: the list of flags, with a **Download CSV** button.
3. **Provider vs peers**: how many other providers of the same role were on shift, against how many patients were waiting; plus % of shift time spent in consults versus peers.
4. **Data quality**: anything that makes results less certain.

Re-running a pull for the same dates simply replaces those days.

Want to look around before connecting anything? `python -m src.demo_data` creates a small **made-up** database (provider `np_demo`, date 2026-03-02). Delete `data/clinician_audit.duckdb` before pulling your real data.

## Optional sources

* **Humanity (shift types):** map Humanity employee IDs to your provider IDs in `config/people.yaml`, then
  `python -m src.pull_humanity --start YYYY-MM-DD --end YYYY-MM-DD` and `python -m src.normalize`.
  The exact shape of your Humanity responses is not confirmed; add `--peek` to see it (names hidden) and see the TODO at the top of `src/pull_humanity.py`.
* **Slack:** set `SLACK_ENABLED=true` and the tokens, list channel IDs in `config/settings.yaml`, map Slack user IDs in `config/people.yaml`. `python -m src.pull_slack --start ... --end ... --peek` prints a few entries on your screen (message text, names and emails are blanked) without saving anything. Message text is never stored.
* **CHR (chart opens):** set `CHR_ENABLED=true`, then add a `chr_events` block (with its Metabase `card_id`) in `config/column_map.yaml`. Run `python -m src.pull_chr --start ... --end ...`.

After pulling any of these, run `python -m src.normalize` (the Metabase pull does this for you).

## Adding a teammate

1. A GitHub org owner adds them to the private repo (Settings > Collaborators).
2. They clone it and follow steps 1 to 3 above, using **their own** Metabase API key in **their own** `.env`. Do not share keys or `.env` files.
3. Share `config/` changes (such as `column_map.yaml`) through Git as normal. Do not share `data/`.

## Putting this folder on GitHub (private)

```bash
git init && git add . && git commit -m "Initial commit"
gh repo create clinician-audit --private --source=. --push
```
Before the first commit, run `git status` and make sure `.env` and `data/` are **not** listed.

## Running the tests

```bash
pytest
```

## Things to know

* All times are converted to **America/Toronto** wall-clock time. Timestamps without a timezone are assumed to be UTC (change `source_timezone` in settings if yours are local). The one repeated hour in November is ambiguous.
* "Patients waiting" = patients in the intake and triaged states, counted per minute and per province. Providers are compared to the province(s) where most of their encounters happen.
* Thresholds (20/20/15 minutes and so on) live in `config/settings.yaml`.
* A flag is a prompt to look closer, not proof of wrongdoing. Check shift notes, outages, and approved exceptions first.
