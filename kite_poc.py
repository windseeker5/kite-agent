#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import argparse
import re
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import unescape
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests


TZ = ZoneInfo("America/Toronto")
SPOT_LAT = 48.544
SPOT_LON = -68.412

PREFERRED_DIRECTIONS = {"SW", "NE"}
FOIL_MIN, FOIL_MAX = 8.0, 16.0
TT_MIN, TT_MAX = 17.0, 35.0
GUST_IDEAL_MAX = 6.0
GUST_ACCEPTABLE_MAX = 10.0
FOIL_HIGH_TIDE_WINDOW_H = 2.0
SURF_STRONG_WIND_THRESHOLD = 25.0
SURF_MEMORY_H = 48
SURF_TRIGGER_DIRS = {"NE", "NW"}


@dataclass
class ForecastHour:
    ts: datetime
    wind_kt: float
    gust_kt: float | None
    wind_deg: float | None
    source: str


@dataclass
class TideEvent:
    ts: datetime
    kind: str
    level_m: float


def cardinal_16(deg: float | None) -> str | None:
    if deg is None:
        return None
    names = [
        "N",
        "NNE",
        "NE",
        "ENE",
        "E",
        "ESE",
        "SE",
        "SSE",
        "S",
        "SSW",
        "SW",
        "WSW",
        "W",
        "WNW",
        "NW",
        "NNW",
    ]
    idx = int((deg + 11.25) // 22.5) % 16
    return names[idx]


def to_knots(ms: float) -> float:
    return ms * 1.943844


def http_get_text(url: str, timeout: int = 20) -> str:
    headers = {"User-Agent": "Mozilla/5.0 kite-poc/1.0"}
    return requests.get(url, headers=headers, timeout=timeout).text


def fetch_open_meteo_forecast(hours: int = 48) -> list[ForecastHour]:
    """Fallback prevision vent robuste quand Windguru n'est pas scrapeable."""
    now = datetime.now(tz=TZ)
    end = now + timedelta(hours=hours)
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={SPOT_LAT}&longitude={SPOT_LON}"
        "&hourly=wind_speed_10m,wind_direction_10m,wind_gusts_10m"
        "&wind_speed_unit=ms"
        "&timezone=America%2FToronto"
    )
    payload = requests.get(url, timeout=20).json()
    h = payload.get("hourly", {})
    out: list[ForecastHour] = []
    for ts, w, d, g in zip(
        h.get("time", []),
        h.get("wind_speed_10m", []),
        h.get("wind_direction_10m", []),
        h.get("wind_gusts_10m", []),
        strict=False,
    ):
        dt = datetime.fromisoformat(ts).replace(tzinfo=TZ)
        if now <= dt <= end:
            out.append(
                ForecastHour(
                    ts=dt,
                    wind_kt=to_knots(float(w)),
                    gust_kt=to_knots(float(g)) if g is not None else None,
                    wind_deg=float(d) if d is not None else None,
                    source="open-meteo",
                )
            )
    return out


def fetch_windguru_or_fallback(hours: int = 48) -> tuple[list[ForecastHour], list[str]]:
    """Essaye Windguru API (session + referer), sinon fallback Open-Meteo."""
    notes: list[str] = []
    try:
        session = requests.Session()
        headers = {
            "User-Agent": "Mozilla/5.0 kite-poc/1.0",
            "Referer": "https://www.windguru.cz/148269",
            "X-Requested-With": "XMLHttpRequest",
        }
        session.get("https://www.windguru.cz/148269", headers=headers, timeout=20)
        meta_url = "https://www.windguru.cz/int/iapi.php?q=forecast_spot&id_spot=148269"
        meta = session.get(meta_url, headers=headers, timeout=20).json()
        tabs = meta.get("tabs", [])
        model_info = None
        for tab in tabs:
            if tab.get("id_model") in (3, 117, 45) and tab.get("id_model_arr"):
                model_info = tab["id_model_arr"][0]
                break
        if model_info is None and tabs and tabs[0].get("id_model_arr"):
            model_info = tabs[0]["id_model_arr"][0]
        if model_info is None:
            raise RuntimeError("Aucun modele Windguru exploitable.")

        fc_url = (
            "https://www.windguru.net/int/iapi.php?q=forecast"
            f"&id_model={model_info['id_model']}"
            f"&rundef={model_info['rundef']}"
            "&id_spot=148269"
            "&WGCACHEABLE=21600"
            f"&cachefix={model_info['cachefix']}"
        )
        fc = session.get(fc_url, headers=headers, timeout=20).json()
        fcst = fc.get("fcst", {})
        initstamp = int(fcst.get("initstamp", 0))
        hours_arr = fcst.get("hours", [])
        wind_arr = fcst.get("WINDSPD", [])
        gust_arr = fcst.get("GUST", [])
        dir_arr = fcst.get("WINDDIR", [])

        out: list[ForecastHour] = []
        now = datetime.now(tz=TZ)
        end = now + timedelta(hours=hours)
        for h, w, g, d in zip(hours_arr, wind_arr, gust_arr, dir_arr, strict=False):
            ts = datetime.fromtimestamp(initstamp + int(h) * 3600, tz=TZ)
            if now <= ts <= end:
                out.append(
                    ForecastHour(
                        ts=ts,
                        wind_kt=float(w),
                        gust_kt=float(g) if g is not None else None,
                        wind_deg=float(d) if d is not None else None,
                        source="windguru",
                    )
                )

        if out:
            notes.append(f"Windguru API OK ({len(out)} points, modele {model_info['id_model']}).")
            return out, notes
        raise RuntimeError("Windguru sans points exploitables.")
    except Exception as exc:
        notes.append(f"Windguru indisponible ({exc}); fallback Open-Meteo utilise.")
    return fetch_open_meteo_forecast(hours), notes


def fetch_tempest_observed_estimate() -> tuple[float | None, float | None, float | None, list[str]]:
    """Lit Tempest via endpoint JSONP utilise par la page partagee."""
    notes: list[str] = []
    try:
        url = (
            "https://swd.weatherflow.com/swd/rest/observations/location"
            "?callback=cb"
            "&api_key=6bff2f89-84ab-463c-886e-fc0f443da4cf"
            "&build=173"
            "&location_id=73230"
            f"&_={int(datetime.now(tz=TZ).timestamp() * 1000)}"
        )
        text = requests.get(
            url,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0 kite-poc/1.0", "Referer": "https://tempestwx.com/"},
        ).text
        m = re.search(r"^cb\((.*)\)$", text, flags=re.S)
        if not m:
            raise RuntimeError("Reponse JSONP invalide")
        payload = json.loads(m.group(1))
        obs_list = payload.get("obs", [])
        if not obs_list:
            raise RuntimeError("Aucune observation")
        obs = obs_list[0]
        avg = float(obs.get("wind_avg")) if obs.get("wind_avg") is not None else None
        gust = float(obs.get("wind_gust")) if obs.get("wind_gust") is not None else None
        direction = float(obs.get("wind_direction")) if obs.get("wind_direction") is not None else None
        if avg is None:
            raise RuntimeError("wind_avg absent")
        notes.append("Tempest observations live OK.")
        return avg, gust, direction, notes
    except Exception as exc:
        notes.append(f"Tempest indisponible ({exc}).")
    return None, None, None, notes


def fetch_tides_gc(days: int = 2) -> tuple[list[TideEvent], list[str]]:
    notes: list[str] = []
    html = http_get_text("https://www.marees.gc.ca/fr/stations/2985")
    text = unescape(re.sub(r"<[^>]+>", " ", html))
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]

    date_re = re.compile(r"^(\d{4}-\d{2}-\d{2})")
    time_re = re.compile(r"^(\d{2}:\d{2})$")
    level_re = re.compile(r"^\d(?:\.\d{1,3})?$")

    events: list[TideEvent] = []
    current_date: str | None = None
    i = 0
    while i < len(lines):
        ln = lines[i]
        dm = date_re.match(ln)
        if dm:
            current_date = dm.group(1)
            i += 1
            continue
        if current_date and time_re.match(ln):
            if i + 1 < len(lines) and level_re.match(lines[i + 1]):
                hhmm = ln
                level = float(lines[i + 1])
                dt = datetime.fromisoformat(f"{current_date}T{hhmm}:00").replace(tzinfo=TZ)
                events.append(TideEvent(ts=dt, kind="unknown", level_m=level))
                i += 2
                continue
        i += 1

    if not events:
        notes.append("Echec parsing marees GC.")
        return [], notes

    events = sorted(events, key=lambda e: e.ts)
    for idx in range(1, len(events) - 1):
        prev_l = events[idx - 1].level_m
        cur_l = events[idx].level_m
        next_l = events[idx + 1].level_m
        if cur_l >= prev_l and cur_l >= next_l:
            events[idx].kind = "high"
        elif cur_l <= prev_l and cur_l <= next_l:
            events[idx].kind = "low"
    if len(events) >= 2:
        events[0].kind = "high" if events[0].level_m > events[1].level_m else "low"
        events[-1].kind = "high" if events[-1].level_m > events[-2].level_m else "low"

    now = datetime.now(tz=TZ)
    max_dt = now + timedelta(days=days)
    filtered = [e for e in events if now - timedelta(hours=12) <= e.ts <= max_dt]
    notes.append(f"Marees GC parsees: {len(filtered)} points.")
    return filtered, notes


def nearest_high_tide_delta_h(ts: datetime, tides: Iterable[TideEvent]) -> float | None:
    highs = [t for t in tides if t.kind == "high"]
    if not highs:
        return None
    nearest = min(highs, key=lambda t: abs((t.ts - ts).total_seconds()))
    return (nearest.ts - ts).total_seconds() / 3600.0


def nearest_high_tide(ts: datetime, tides: Iterable[TideEvent]) -> TideEvent | None:
    highs = [t for t in tides if t.kind == "high"]
    if not highs:
        return None
    return min(highs, key=lambda t: abs((t.ts - ts).total_seconds()))


def within_range(x: float, low: float, high: float) -> bool:
    return low <= x <= high


def score_direction(deg: float | None) -> tuple[float, str]:
    card = cardinal_16(deg)
    if card in PREFERRED_DIRECTIONS:
        return 1.0, f"direction {card} préférée"
    if card is None:
        return 0.5, "direction inconnue"
    return 0.4, f"direction {card} moins idéale"


def score_gust(avg_kt: float, gust_kt: float | None) -> tuple[float, str]:
    if gust_kt is None:
        return 0.5, "rafales indisponibles"
    spread = max(0.0, gust_kt - avg_kt)
    if spread <= GUST_IDEAL_MAX:
        return 1.0, f"rafales +{spread:.1f} kt (idéal)"
    if spread <= GUST_ACCEPTABLE_MAX:
        return 0.7, f"rafales +{spread:.1f} kt (acceptable)"
    return 0.25, f"rafales +{spread:.1f} kt (agite)"


def discipline_score(
    discipline: str,
    hour: ForecastHour,
    tides: list[TideEvent],
    forecast: list[ForecastHour],
) -> tuple[float, list[str]]:
    reasons: list[str] = []
    dir_s, dir_r = score_direction(hour.wind_deg)
    gust_s, gust_r = score_gust(hour.wind_kt, hour.gust_kt)
    reasons.extend([dir_r, gust_r])

    wind_s = 0.0
    tide_s = 0.5
    mem_s = 0.5

    if discipline == "foil":
        if within_range(hour.wind_kt, FOIL_MIN, FOIL_MAX):
            wind_s = 1.0
            reasons.append(f"vent {hour.wind_kt:.1f} kt dans la plage foil")
        else:
            d = min(abs(hour.wind_kt - FOIL_MIN), abs(hour.wind_kt - FOIL_MAX))
            wind_s = max(0.0, 1.0 - d / 8.0)
            reasons.append(f"vent {hour.wind_kt:.1f} kt hors plage foil")

        delta_h = nearest_high_tide_delta_h(hour.ts, tides)
        if delta_h is None:
            tide_s = 0.4
            reasons.append("marée indisponible")
        elif abs(delta_h) <= FOIL_HIGH_TIDE_WINDOW_H:
            tide_s = 1.0
            reasons.append(f"proche marée haute ({delta_h:+.1f}h)")
        else:
            tide_s = 0.45
            reasons.append(f"loin de marée haute ({delta_h:+.1f}h)")

    elif discipline == "twintip":
        if within_range(hour.wind_kt, TT_MIN, TT_MAX):
            wind_s = 1.0
            reasons.append(f"vent {hour.wind_kt:.1f} kt dans la plage twin-tip")
        else:
            d = min(abs(hour.wind_kt - TT_MIN), abs(hour.wind_kt - TT_MAX))
            wind_s = max(0.0, 1.0 - d / 10.0)
            reasons.append(f"vent {hour.wind_kt:.1f} kt hors plage twin-tip")
        tide_s = 0.6

    else:  # surf
        wind_s = 0.55
        recent = [p for p in forecast if hour.ts - timedelta(hours=SURF_MEMORY_H) <= p.ts <= hour.ts]
        trigger = 0
        for p in recent:
            c = cardinal_16(p.wind_deg)
            if p.wind_kt >= SURF_STRONG_WIND_THRESHOLD and c in SURF_TRIGGER_DIRS:
                trigger += 1
        if trigger >= 3:
            mem_s = 1.0
            reasons.append("historique fort vent NE/NW favorable à la houle")
        elif trigger > 0:
            mem_s = 0.7
            reasons.append("début de séquence vent NE/NW")
        else:
            mem_s = 0.35
            reasons.append("pas de séquence vent NE/NW notable")

    score = 100.0 * (0.45 * wind_s + 0.20 * dir_s + 0.20 * gust_s + 0.15 * ((tide_s + mem_s) / 2.0))
    return score, reasons


def confidence_label(quality: float) -> str:
    if quality >= 0.8:
        return "Haute"
    if quality >= 0.55:
        return "Moyenne"
    return "Basse"


def choose_best_windows(forecast: list[ForecastHour], tides: list[TideEvent]) -> list[dict[str, Any]]:
    return choose_best_windows_horizon(forecast, tides, horizon_hours=24, top_n=3)


def choose_best_windows_horizon(
    forecast: list[ForecastHour],
    tides: list[TideEvent],
    horizon_hours: int,
    top_n: int,
) -> list[dict[str, Any]]:
    now = datetime.now(tz=TZ).replace(minute=0, second=0, microsecond=0)
    end = now + timedelta(hours=horizon_hours)
    in_scope = [p for p in forecast if now <= p.ts <= end]

    out: list[dict[str, Any]] = []
    for p in in_scope:
        best = None
        for d in ("foil", "twintip", "surf"):
            s, reasons = discipline_score(d, p, tides, forecast)
            candidate = {"ts": p.ts, "discipline": d, "score": s, "reasons": reasons, "pt": p}
            if best is None or s > best["score"]:
                best = candidate
        if best:
            out.append(best)

    out.sort(key=lambda x: x["score"], reverse=True)
    selected: list[dict[str, Any]] = []
    used_hours: set[datetime] = set()
    for cand in out:
        hour_key = cand["ts"]
        if hour_key in used_hours:
            continue
        selected.append(cand)
        used_hours.add(hour_key)
        if len(selected) == top_n:
            break
    return selected


def print_report(
    top: list[dict[str, Any]],
    source_notes: list[str],
    forecast: list[ForecastHour],
    tides: list[TideEvent],
    tempest_avg: float | None,
    tempest_gust: float | None,
) -> None:
    now = datetime.now(tz=TZ)
    has_obs = tempest_avg is not None
    has_forecast = bool(forecast)
    quality = 0.4 + (0.3 if has_obs else 0.0) + (0.3 if has_forecast else 0.0)
    conf = confidence_label(quality)

    print("=" * 72)
    print("AI Agent Kite Report - Rimouski (FR)".center(72))
    print(now.strftime("Généré le %Y-%m-%d à %H:%M (%Z)").center(72))
    print("=" * 72)
    if has_obs:
        print(f"Observations vent (Tempest): moyenne ~{tempest_avg:.1f} kt, rafale ~{tempest_gust:.1f} kt")
    else:
        print("Observations vent (Tempest): non disponibles en HTTP simple (source JS).")
    print(f"Confiance globale: {conf}")
    upcoming_highs = [t for t in tides if t.kind == "high" and t.ts >= now]
    if upcoming_highs:
        print("Prochaines marées hautes:")
        for t in upcoming_highs[:2]:
            print(f"- {t.ts.strftime('%a %H:%M')} | niveau {t.level_m:.2f} m")
    else:
        print("Prochaines marées hautes: indisponibles")
    print()
    print("Top 3 créneaux (prochaines 24h):")

    if not top:
        print("- Aucun creneau calcule.")
    for i, row in enumerate(top, start=1):
        p: ForecastHour = row["pt"]
        ts = row["ts"].strftime("%a %H:%M")
        dir_txt = cardinal_16(p.wind_deg) or "?"
        gust_txt = f"{p.gust_kt:.1f}" if p.gust_kt is not None else "n/a"
        high = nearest_high_tide(row["ts"], tides)
        tide_txt = "marée n/a"
        if high is not None:
            delta_h = (high.ts - row["ts"]).total_seconds() / 3600.0
            tide_txt = f"marée haute {high.ts.strftime('%H:%M')} ({delta_h:+.1f}h), niveau {high.level_m:.2f} m"
        print(
            f"{i}) {ts} | {row['discipline']} | {row['score']:.0f}/100 | "
            f"vent {p.wind_kt:.1f} kt, rafale {gust_txt} kt, dir {dir_txt}"
        )
        print(f"   Pourquoi: {row['reasons'][0]}; {row['reasons'][1]}; {row['reasons'][2]}")
        print(f"   Marée: {tide_txt}")

    print()
    print("Préférences utilisateur appliquées:")
    print("- Fuseau: America/Toronto (Montreal)")
    print("- Directions préférées: SW, NE")
    print("- Foil: 8-16 kt")
    print("- Foil favorisé à +/-2h de la marée haute")
    print("- Rafales foil: idéal <= +6 kt, acceptable <= +10 kt")
    print("- Twin-tip: 17-35 kt")
    print("- Surf: favorisé après séquence de vent fort NE/NW")
    print("=" * 72)


def build_report_text(
    top: list[dict[str, Any]],
    tides: list[TideEvent],
    forecast: list[ForecastHour],
    tempest_avg: float | None,
    tempest_gust: float | None,
) -> str:
    lines: list[str] = []
    now = datetime.now(tz=TZ)
    has_obs = tempest_avg is not None
    has_forecast = bool(forecast)
    quality = 0.4 + (0.3 if has_obs else 0.0) + (0.3 if has_forecast else 0.0)
    conf = confidence_label(quality)
    lines.append("=" * 72)
    lines.append("AI Agent Kite Report - Rimouski (FR)".center(72))
    lines.append(now.strftime("Généré le %Y-%m-%d à %H:%M (%Z)").center(72))
    lines.append("=" * 72)
    if has_obs:
        lines.append(f"Observations vent (Tempest): moyenne ~{tempest_avg:.1f} kt, rafale ~{tempest_gust:.1f} kt")
    else:
        lines.append("Observations vent (Tempest): non disponibles en HTTP simple (source JS).")
    lines.append(f"Confiance globale: {conf}")
    upcoming_highs = [t for t in tides if t.kind == "high" and t.ts >= now]
    if upcoming_highs:
        lines.append("Prochaines marées hautes:")
        for t in upcoming_highs[:2]:
            lines.append(f"- {t.ts.strftime('%a %H:%M')} | niveau {t.level_m:.2f} m")
    else:
        lines.append("Prochaines marées hautes: indisponibles")
    lines.append("")
    lines.append("Top 3 créneaux (prochaines 24h):")
    if not top:
        lines.append("- Aucun créneau calculé.")
    for i, row in enumerate(top, start=1):
        p: ForecastHour = row["pt"]
        ts = row["ts"].strftime("%a %H:%M")
        dir_txt = cardinal_16(p.wind_deg) or "?"
        gust_txt = f"{p.gust_kt:.1f}" if p.gust_kt is not None else "n/a"
        high = nearest_high_tide(row["ts"], tides)
        tide_txt = "marée n/a"
        if high is not None:
            delta_h = (high.ts - row["ts"]).total_seconds() / 3600.0
            tide_txt = f"marée haute {high.ts.strftime('%H:%M')} ({delta_h:+.1f}h), niveau {high.level_m:.2f} m"
        lines.append(
            f"{i}) {ts} | {row['discipline']} | {row['score']:.0f}/100 | "
            f"vent {p.wind_kt:.1f} kt, rafale {gust_txt} kt, dir {dir_txt}"
        )
        lines.append(f"   Pourquoi: {row['reasons'][0]}; {row['reasons'][1]}; {row['reasons'][2]}")
        lines.append(f"   Marée: {tide_txt}")
    lines.append("")
    lines.append("Préférences utilisateur appliquées:")
    lines.append("- Fuseau: America/Toronto (Montreal)")
    lines.append("- Directions préférées: SW, NE")
    lines.append("- Foil: 8-16 kt")
    lines.append("- Foil favorisé à +/-2h de la marée haute")
    lines.append("- Rafales foil: idéal <= +6 kt, acceptable <= +10 kt")
    lines.append("- Twin-tip: 17-35 kt")
    lines.append("- Surf: favorisé après séquence de vent fort NE/NW")
    lines.append("=" * 72)
    return "\n".join(lines)


def post_discord(webhook_url: str, content: str) -> None:
    payload = {"content": content[:1900]}
    resp = requests.post(webhook_url, json=payload, timeout=20)
    resp.raise_for_status()


def build_discord_alert_markdown(
    top: list[dict[str, Any]],
    tides: list[TideEvent],
    tempest_avg: float | None,
    tempest_gust: float | None,
    tempest_dir: float | None,
) -> str:
    now = datetime.now(tz=TZ)
    lines: list[str] = []
    lines.append("## AI Agent Kite Report - Rimouski")
    lines.append(f"🕒 *{now.strftime('%a %Y-%m-%d %H:%M %Z')}*")

    if tempest_avg is not None:
        dir_txt = cardinal_16(tempest_dir) or "?"
        gust_txt = f"{tempest_gust:.1f}" if tempest_gust is not None else "n/a"
        lines.append(f"🌬️ **Live Tempest**: {tempest_avg:.1f} kt (rafale {gust_txt} kt) • {dir_txt}")
    else:
        lines.append("🌬️ **Live Tempest**: indisponible")


    high = next((t for t in tides if t.kind == "high" and t.ts >= now), None)
    if high is not None:
        lines.append(f"🌊 **Prochaine marée haute**: {high.ts.strftime('%a %H:%M')} ({high.level_m:.2f} m)")

    lines.append("")
    lines.append("### Top opportunités (24h)")
    if not top:
        lines.append("- Aucun créneau solide détecté")
    for row in top[:3]:
        p: ForecastHour = row["pt"]
        gear = {"foil": "🪁 Foil", "twintip": "🏄 Twin-tip", "surf": "🌊 Surf"}.get(row["discipline"], row["discipline"])
        dir_txt = cardinal_16(p.wind_deg) or "?"
        gust_txt = f"{p.gust_kt:.1f}" if p.gust_kt is not None else "n/a"
        lines.append(
            f"- **{p.ts.strftime('%a %H:%M')}** • **{row['score']:.0f}** • {gear} • {p.wind_kt:.1f}kt g{gust_txt} • {dir_txt}"
        )

    lines.append("")
    lines.append("_Règles Rimouski: Foil 8-16 kt (+/-2h marée haute), Twin-tip 17-35 kt, dirs SW/NE._")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Agent Kite Report")
    parser.add_argument("--monitor", action="store_true", help="N'affiche/envoie que si score >= seuil")
    parser.add_argument("--threshold", type=float, default=80.0, help="Seuil minimum de score pour alerte")
    parser.add_argument("--horizon-hours", type=int, default=72, help="Fenêtre de recherche d'opportunité")
    parser.add_argument("--discord-webhook", type=str, default="", help="Webhook Discord pour envoi")
    args = parser.parse_args()

    notes: list[str] = []
    tides, tide_notes = fetch_tides_gc(days=2)
    notes.extend(tide_notes)

    fetch_horizon = max(48, args.horizon_hours)
    forecast, wind_notes = fetch_windguru_or_fallback(hours=fetch_horizon)
    notes.extend(wind_notes)
    notes.append(f"Prevision source: {forecast[0].source if forecast else 'aucune'} ({len(forecast)} points)")

    obs_avg, obs_gust, obs_dir, tempest_notes = fetch_tempest_observed_estimate()
    notes.extend(tempest_notes)

    top = choose_best_windows(forecast, tides)

    if args.monitor:
        candidates = choose_best_windows_horizon(
            forecast,
            tides,
            horizon_hours=max(1, args.horizon_hours),
            top_n=5,
        )
        qualified = [c for c in candidates if c["score"] >= args.threshold]
        if not qualified:
            return 0
        report = build_discord_alert_markdown(top, tides, obs_avg, obs_gust, obs_dir)
        if args.discord_webhook:
            post_discord(args.discord_webhook, report)
        else:
            print(report)
        return 0

    print_report(top, notes, forecast, tides, obs_avg, obs_gust)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrompu.")
        raise SystemExit(130)
