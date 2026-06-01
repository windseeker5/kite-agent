# Kite Agent

Proof-of-concept kite alert tool for the Rimouski area.

## What it does

- Pulls live wind from Tempest
- Pulls forecast from Windguru (with fallback)
- Pulls tides from Marees GC
- Scores upcoming windows for foil/twin-tip/surf
- Sends compact Discord alerts when score threshold is met

## Run

```bash
python kite_poc.py
```

## Alert mode

```bash
python kite_poc.py --monitor --threshold 80 --horizon-hours 72 --discord-webhook "YOUR_WEBHOOK_URL"
```
