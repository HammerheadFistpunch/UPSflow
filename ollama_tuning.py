"""Generate and validate an Ollama reserve recommendation."""
import json
from http.client import HTTPException
from math import ceil
from statistics import median
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from efficiency import MAX_CORRECTION_WATTS, valid_correction
from long_term import matching_days, recommendation


def ollama_settings(config):
    settings = config.get("ollama_tuning")
    if settings is None:
        return None
    if not isinstance(settings, dict):
        raise SystemExit("ollama_tuning must be a JSON object")
    url, model = settings.get("url"), settings.get("model")
    if isinstance(url, str):
        try:
            parts = urlsplit(url)
            if (parts.scheme in {"http", "https"} and parts.hostname
                    and parts.username is None and not parts.query and not parts.fragment
                    and parts.path in {"", "/"} and isinstance(model, str) and model.strip()):
                return url.rstrip("/"), model.strip()
        except ValueError:
            pass
    raise SystemExit("ollama_tuning requires an HTTP(S) base URL and a model name")


def choose_reserve(settings, daily, device, month, reserve, limit, minimum, maximum):
    args = daily, device, month, reserve, limit, minimum, maximum
    fallback, reason = recommendation(*args)
    if settings is None:
        return fallback, f"Rules: {reason}"
    rows = matching_days(*args[:5])
    upper = min(maximum, ceil(limit) - 1)
    if len(rows) < 14 or not minimum <= reserve <= maximum or upper < minimum:
        return fallback, f"Rules (Ollama skipped: insufficient matching history or reserve outside bounds): {reason}"

    url, model = settings
    summary = {
        "current_reserve_percent": reserve,
        "charge_ceiling_percent": limit,
        "minimum_reserve_percent": minimum,
        "maximum_reserve_percent": upper,
        "days": [
            {key: row[key] for key in (
                "date", "coverage", "battery_min", "battery_max",
                "solar_wh", "ac_wh", "output_wh", "tuning_signal",
            ) if key in row}
            for row in rows
        ],
    }
    body = {
        "model": model,
        "stream": False,
        "think": False,
        "options": {"temperature": 0, "num_predict": 256},
        "format": {
            "type": "object",
            "properties": {
                "reserve_percent": {"type": "integer", "minimum": minimum, "maximum": upper},
                "reason": {"type": "string"},
            },
            "required": ["reserve_percent", "reason"],
        },
        "messages": [
            {"role": "system", "content": (
                "Recommend the monthly Energy Backup reserve for one solar-powered UPS. "
                "Balance outage reserve, solar capture, and grid energy. Leave headroom if the battery "
                "consistently fills early; low daily peaks alone do not justify additional grid charging. "
                "Daily energy figures are observed estimates; early_full means the ceiling was reached "
                "within three hours of solar starting and solar continued afterward. "
                "Keep the current reserve if the evidence is mixed. Return one integer percentage "
                "inside the supplied bounds and a concise explanation based on the data."
            )},
            {"role": "user", "content": json.dumps(summary)},
        ],
    }
    try:
        result = chat_json(url, body)
        proposed, explanation = result["reserve_percent"], result["reason"]
        if (type(proposed) is not int or not minimum <= proposed <= upper
                or not isinstance(explanation, str) or not explanation.strip()):
            raise ValueError("invalid recommendation")
    except HTTPError as exc:
        failure = f"HTTP {exc.code}"
    except (URLError, OSError, HTTPException):
        failure = "connection failed or timed out"
    except (ValueError, KeyError, TypeError):
        failure = "invalid or out-of-bounds recommendation"
    else:
        return proposed, f"Ollama ({model}): {' '.join(explanation.split())[:400]}"
    return fallback, f"Rules fallback (Ollama failed: {failure}): {reason}"


def chat_json(url, body):
    request = Request(url + "/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urlopen(request, timeout=60) as response:
        reply = json.loads(response.read(65536))
    return json.loads(reply["message"]["content"])


def choose_efficiency(settings, days, current):
    url, model = settings
    reference = median(day["net_watts"] for day in days)
    lower = max(0, round(reference - 2, 2))
    upper = min(MAX_CORRECTION_WATTS, round(reference + 2, 2))
    body = {
        "model": model,
        "stream": False,
        "think": False,
        "options": {"temperature": 0, "num_predict": 256},
        "format": {
            "type": "object",
            "properties": {
                "correction_watts": {"type": "number", "minimum": lower, "maximum": upper},
                "reason": {"type": "string"},
            },
            "required": ["correction_watts", "reason"],
        },
        "messages": [
            {"role": "system", "content": (
                "Calibrate an apparent power loss in watts for one UPS on DC input with "
                "its AC inverter enabled. Corrected net power equals reported net minus "
                "this loss. Each day summarizes near-hour windows of steady reported "
                "state of charge. Choose a correction within the supplied bounds that "
                "brings typical net power close to zero. Use the median as the default. "
                "These are estimated losses at the observed loads. If evidence is mixed, "
                "explain why. Return the correction and a concise reason."
            )},
            {"role": "user", "content": json.dumps({
                "current_correction_watts": current, "median_net_watts": reference,
                "minimum_watts": lower, "maximum_watts": upper, "days": days,
            })},
        ],
    }
    try:
        result = chat_json(url, body)
        proposed, reason = result["correction_watts"], result["reason"]
        if (not valid_correction(proposed) or not lower <= proposed <= upper
                or not isinstance(reason, str) or not reason.strip()):
            raise ValueError("invalid recommendation")
    except HTTPError as exc:
        failure = f"HTTP {exc.code}"
    except (URLError, OSError, HTTPException):
        failure = "connection failed or timed out"
    except (ValueError, KeyError, TypeError):
        failure = "invalid or out-of-bounds recommendation"
    else:
        return round(proposed, 2), f"Ollama ({model}): {' '.join(reason.split())[:400]}"
    return None, f"Ollama failed: {failure}. Kept the existing correction."
