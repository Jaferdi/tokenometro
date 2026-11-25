# Tokenometro

*[Leer en español](README.es.md)*

A local dashboard that shows what your Claude Code sessions "cost", where that spend goes, and what to change to spend less. It works with any way of using Claude Code (Pro, Max, Team, Enterprise or API): it reads the logs Claude Code already keeps on your machine and prices them at API rates.

> Not affiliated with Anthropic. Claude and Claude Code are trademarks of Anthropic.

## Privacy

- It only reads `~/.claude/projects/**/*.jsonl` (and, if present, the last reading of the sub-meter plugin).
- It listens on `127.0.0.1`: the dashboard is not reachable from outside your machine.
- It never goes online or sends anything. Everything stays in `~/.local/share/tokenometro/usage.db` (SQLite).
- "Biggest calls" masks anything that looks like a secret, but the dashboard does show parts of your commands and paths: check before sharing screenshots.

## Requirements

Python 3.8 or later, no dependencies. Linux, WSL or macOS.

## Install

```bash
git clone https://github.com/Jaferdi/tokenometro.git && cd tokenometro
bash install.sh               # installs, registers auto-start and launches it
bash install.sh --statusline  # also uses statusline.py to see plan usage %
```

The installer:

1. Copies the files to `~/.local/share/tokenometro`.
2. Adds a `SessionStart` hook to `~/.claude/settings.json` that starts the dashboard when you open Claude Code. It backs up the file first: `settings.json.bak-tokenometro-<date>`.
3. Reads your history (the first run takes a while) and starts the server at http://127.0.0.1:7878.

If you had the earlier version in `~/.local/share/claude-tokens`, it copies your data and settings and swaps the hook.

To remove it: `bash ~/.local/share/tokenometro/install.sh --uninstall` (removes the hook and our statusLine; data stays until you delete the folder).

## What the dashboard shows

- **Today and right now**: today's spend (odometer) and open sessions with their context size, running cost, compactions and, when plan usage is available, how much of the 5-hour window and the week each one took.
- **Plan**: % used of the 5-hour window and of the week, when it resets, and an estimated ceiling in equivalent € or $ (see below).
- **Recommended improvements**: cards with what to change, how much it weighs and how to check it worked.
- **Sessions**: cost per session broken down (input, output, cache reads and cache writes), the most cost-effective model for each, where the spend went and the tool that added the most text. Click a row for details: segments between compactions, subagents and biggest calls.
- **By model and by client**, and **Before / after** a date to measure whether a settings change helped.

The top bar picks period, client, currency (€ or $), language (English or Spanish) and theme (system, light or dark). Tables are paginated.

### "€ eq." / "$ eq."

What those tokens would cost at API rates (`prices.json`). On a subscription you don't pay that, but it lets you compare sessions and see what weighs most. On the API it is roughly your bill: fast mode (twice the price), Batch, and Haiku 5.5 above 100K prompt tokens are not modelled. On Bedrock, Vertex or Foundry each provider sets its own prices: adjust `prices.json`. Calculations are done in dollars, the official rate, and converted to euros for display using the rate in `prices.json`.

## Recommended improvements: how they are built and how they evolve

Each card comes from a rule that crosses your data for the period with your actual configuration (`settings.json`, enabled hooks and plugins). It only shows up when the rule has something to say. Each card has:

- **Status**: *to do* (not applied), *no clear effect* (applied but the data isn't improving), *good* (applied and it shows) or *info*.
- **Impact**: rough amount for the period and how it was calculated.
- **Metric and history**: every hour a daily snapshot of all cards is saved (`recs_hist`). That feeds the squares for the last 14 days, "to do since…" or "good since…", and the trend versus 7 days ago. If a card changes while the dashboard is open, it flashes.
- **Before / after**: some rules compare against the cut-off date. Until there are 7 days since the cut-off the card shows "Under observation", because with fewer days the kind of task weighs more than the change.
- **With what**: existing tools or settings that are worth it to fix it.
- **Prompt**: the text to ask Claude to do it is saved to `~/.claude/reports/improvements-YYYY-MM-DD.md` (`mejoras-…` in Spanish) and copied with "Copy prompt".

Current rules: oversized context and compactions, resuming sessions after long pauses (rewrites the cache), subagents running on Opus, reading huge files, showing context in the status line (only if you don't already have a statusLine or a plugin showing it), model output (only above 25% of cost) and cost-effective model.

Limits: these are heuristics over your logs. They can't tell whether a session really needed more context or a bigger model; they tell you where to look.

## Cost-effective model per session

For each session that used Opus, it looks at what the session did:

| If… | Suggests |
|---|---|
| no code edits, ≤ 10 turns and under $2 | Haiku |
| no code edits | Sonnet |
| 1 to 9 code files edited with fewer than 5 Bash errors | opusplan (Opus plans, Sonnet executes) |
| anything else | Opus was right |

Edits to documentation (`.md`, `.json`, `.yaml`…) don't count as code. The saving is an upper bound: the same tokens at the smaller model's price, although a smaller model may need more turns.

## Plan usage (5 hours and week)

Plan usage only exists on subscriptions (Pro, Max, Team…): the API has no 5-hour or weekly limits, so this card just shows what was spent. The % is not in the logs. Claude Code receives it from Anthropic and only passes it to the statusLine and to plugins. Tokenometro reads it from:

1. **The sub-meter plugin**, if you have it: it saves its last reading in `~/.claude/plugins/store/sub-meter_*.json`.
2. **`statusline.py`**, included here: it reads the JSON Claude Code sends to the statusLine (`rate_limits`) and saves it to `~/.local/share/tokenometro/plan.json`. Enable it with `install.sh --statusline`. If you already have a statusLine, chain it:

   ```
   python3 "$HOME/.local/share/tokenometro/statusline.py" --tee | <your current command>
   ```

It uses whichever reading is newer. With neither, the dashboard only shows what was spent in the last 5 hours and 7 days.

**Estimated ceiling**: each reading is stored together with the spend in that window. The ceiling is the median of *spent × 100 / %* (readings of at least 10%). It's approximate: the % includes what you use on claude.ai or on another machine, and Anthropic doesn't publish how each model is weighted.

## Configuration

Everything lives in `~/.local/share/tokenometro/`. Restart after editing (see below).

### `clients.json`

Groups sessions by client based on the folder you worked in (the first matching path wins). Start from `clients.example.json`:

- `clients`: name → list of path fragments.
- `cut` (optional): YYYY-MM-DD date of the change you want to measure in "Before / after". Defaults to 7 days ago.

The Spanish names from earlier versions (`clientes.json`, `clientes`, `corte`) still work.

### `prices.json`

Price per million tokens in dollars (official API rates) for current and previous models (Fable, Mythos, Opus, Sonnet and Haiku): input, output, cache read, and cache write at 5 minutes and 1 hour. The name is matched inside the model id and the longest match wins, so `claude-opus-5` isn't confused with `claude-opus-5-5`, and Bedrock or Vertex prefixes work. `_default` applies to models not in the list. Review it when Anthropic changes prices or releases models.

`_currency` controls euros:

```json
"_currency": {"default": "USD", "eur_per_usd": 0.91, "date": "2026-11-19"}
```

Update the rate by hand: Tokenometro never goes online. If your `prices.json` comes from an earlier version without this block, the dashboard only offers dollars; copy it from the repo.

### Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `TOKENOMETRO_PORT` | Dashboard port | 7878 |
| `TOKENOMETRO_COST_WARN` | Warn when a session goes over this amount (in $) | 50 |
| `TOKENOMETRO_CUT` | "Before / after" cut-off date | the one in `clients.json` or 7 days ago |
| `TOKENOMETRO_CURRENCY` | Default `EUR` or `USD` | the one in `prices.json` |
| `TOKENOMETRO_LANG` | Default `en` or `es` | `en` |
| `TOKENOMETRO_NO_START` | `1` so `install.sh` doesn't start it | |

The old `CLAUDE_TOKENS_*` names still work.

## Command line

```bash
python3 ~/.local/share/tokenometro/tracker.py            # server (what the hook runs)
python3 ~/.local/share/tokenometro/tracker.py ingest     # read new logs and print a summary
python3 ~/.local/share/tokenometro/tracker.py backfill   # fill in tool data for old history (once)
```

Restart the server (kills only the process listening on the port):

```bash
kill $(ss -ltnpH 'sport = :7878' | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2)
~/.local/share/tokenometro/start.sh
```

## Files

| File | What it is |
|---|---|
| `tracker.py` | Reader, database, rules and dashboard (HTML included) |
| `start.sh` | Starts it in the background unless it's already running |
| `statusline.py` | Optional statusLine that records plan usage |
| `install.sh` | Install, update and uninstall |
| `usage.db` | Data (rebuilt from the logs if you delete it) |
| `plan.json` | Last plan reading from `statusline.py` |
| `tracker.log` | Server log |

## License

MIT. See [LICENSE](LICENSE). To contribute, see [CONTRIBUTING.md](CONTRIBUTING.md).
