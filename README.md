# AI Usage Dashboard

A local, live dashboard of how much AI you're using on your own PC. It reads the logs your AI tools already keep, works out tokens, pace and estimated value, and serves a self-refreshing web page on `localhost`. It never writes to those logs and nothing leaves your machine.

Single Python file plus one HTML page. **Standard library only**: no installs.

## What it shows
- **Accounts**: Claude plan limits (5-hour and weekly windows, per-model caps), tokens today, a bar per day, and how fresh each reading is. Ollama gets a traffic-only row.
- **Pace**: weekly usage against how much of the week has passed, plus the burn rate over the last hour.
- **Sessions**: every Claude Code session with its model, activity, tokens and estimated value.
- **Connections**: which session fed which model. Thicker lines mean more tokens; moving dashes mean live.
- **Totals**: tokens and value by model, prompt-cache share, active minutes.

## Where the numbers come from
| Source | Path | What it gives |
|---|---|---|
| Claude Code transcripts | `~/.claude/projects/**/*.jsonl` | tokens per call, including cache reads and writes |
| Claude plan limits | `%APPDATA%/Claude/plan-usage-history.json` and `plan-readings.json` | plan-limit percentages |
| Ollama server logs | `%LOCALAPPDATA%/Ollama/server*.log` | calls and durations (no token counts) |

Dollar values are estimates at published API list prices (the `PRICES` table at the top of `server.py`), not actual charges.

## Run it
```bash
python server.py --open
```
Or double-click `Start dashboard.cmd` on Windows. It serves http://127.0.0.1:8377/ and refreshes every 10 seconds. Use `--port` to change the port.

To record plan-limit readings by hand, copy `plan-readings.example.json` to `plan-readings.json` and add entries. The file is git-ignored.

## How it works
- `server.py` scans the sources on each request (with incremental caching of transcript files), aggregates them into one JSON document at `/api/data`, and serves `dashboard.html`.
- `dashboard.html` is plain HTML, CSS and JavaScript with no framework: it polls the API and draws the bars, pace gauge and session-to-model graph.
- The server binds to `127.0.0.1` only.

## Limits
- Plan-limit percentages are the last reading, not a live feed; a reading is greyed out once its window resets.
- Usage on other devices or in the web chat counts toward plan limits but doesn't appear in token counts.
- Tools without local logs (most web chat apps) can't be shown.

## License
MIT. See [LICENSE](LICENSE).
