# Kite Agent

Proof-of-concept kite alert tool for the Rimouski area.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## What it does

- Pulls live wind from Tempest
- Pulls forecast from Windguru (with fallback)
- Pulls tides from Marees GC
- Scores upcoming windows for foil/twin-tip/surf
- Sends compact Discord alerts when score threshold is met

## Run

```bash
python kite_poc.py --config config/rimouski.json
```

## Alert mode

```bash
python kite_poc.py --monitor
```

Alert settings come from `config/rimouski.json`. The default Rimouski alert horizon is 7 days. Put secrets in an ignored local override:

```json
{
  "alert": {
    "discord_webhook": "YOUR_WEBHOOK_URL"
  }
}
```

Save that as `config/rimouski.local.json`. It is automatically merged over `config/rimouski.json` and is ignored by git.

## Config

Spot/provider/preferences/alert settings are loaded from JSON config files.

- Current config: `config/rimouski.json`
- Local secrets: `config/rimouski.local.json`
- Future spots: add new files (example `config/iles-madeleine.json`)
