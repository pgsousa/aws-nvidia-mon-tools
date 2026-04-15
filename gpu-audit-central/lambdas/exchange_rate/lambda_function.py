import json
import logging
import os
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import boto3

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

SSM = boto3.client("ssm")

EXCHANGE_RATE_PARAMETER = os.environ["EXCHANGE_RATE_PARAMETER"]
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"


def _fetch_usd_to_eur_rate() -> Decimal:
    with urllib.request.urlopen(ECB_URL, timeout=10) as response:
        root = ET.fromstring(response.read())

    namespace = {
        "gesmes": "http://www.gesmes.org/xml/2002-08-01",
        "def": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref",
    }
    for cube in root.findall(".//def:Cube[@currency='USD']", namespace):
        return Decimal(cube.attrib["rate"])
    raise RuntimeError("ECB USD exchange rate was not found")


def _get_cached_rate() -> Decimal | None:
    try:
        response = SSM.get_parameter(Name=EXCHANGE_RATE_PARAMETER)
        return Decimal(response["Parameter"]["Value"])
    except SSM.exceptions.ParameterNotFound:
        return None
    except Exception as exc:
        LOGGER.warning("Failed to get cached rate: %s", exc)
        return None


def _cache_rate(rate: Decimal) -> None:
    SSM.put_parameter(
        Name=EXCHANGE_RATE_PARAMETER,
        Value=str(rate),
        Type="String",
        Overwrite=True,
        Description="USD to EUR exchange rate from ECB (cached daily)",
    )


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    LOGGER.info("Received event: %s", json.dumps(event))

    try:
        previous_rate = _get_cached_rate()
        rate = _fetch_usd_to_eur_rate()
        _cache_rate(rate)

        change_pct = None
        if previous_rate and previous_rate != rate:
            change_pct = float(((rate - previous_rate) / previous_rate) * 100)

        return {
            "status": "success",
            "rate": str(rate),
            "previous_rate": str(previous_rate) if previous_rate else None,
            "change_percentage": change_pct,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    except Exception as exc:
        LOGGER.error("Failed to update exchange rate: %s", exc)
        cached = _get_cached_rate()
        if cached:
            return {
                "status": "fallback-to-cache",
                "rate": str(cached),
                "error": str(exc),
                "timestamp": datetime.now(UTC).isoformat(),
            }
        raise
