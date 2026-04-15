import json
import logging
import os
import urllib.parse
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import boto3
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

AWS_REGION = os.environ["AWS_REGION"]
GOOGLE_SHEETS_SPREADSHEET_ID = os.environ["GOOGLE_SHEETS_SPREADSHEET_ID"]
RUNNING_COSTS_SHEET_RANGE = os.environ["RUNNING_COSTS_SHEET_RANGE"]
GOOGLE_WRITER_SECRET_ARN = os.environ["GOOGLE_WRITER_SECRET_ARN"]
DEFAULT_CREATED_BY = os.environ.get("DEFAULT_CREATED_BY", "unknown")
DEFAULT_ROOT_VOLUME_SIZE_GIB = int(os.environ.get("ROOT_VOLUME_SIZE_GIB", "64"))
EXCHANGE_RATE_PARAMETER = os.environ["EXCHANGE_RATE_PARAMETER"]

EC2 = boto3.client("ec2")
PRICING = boto3.client("pricing", region_name="us-east-1")
SECRETS = boto3.client("secretsmanager")
SSM = boto3.client("ssm")

EBS_GP3_PRICE_PER_GB_MONTH = {
    "us-east-1": Decimal("0.08"),
    "us-west-2": Decimal("0.08"),
    "eu-west-1": Decimal("0.092"),
    "eu-west-2": Decimal("0.096"),
    "eu-central-1": Decimal("0.099"),
}

MANAGED_TAGS = {
    "ManagedBy": "terraform",
    "Stack": "aws-gpu-test",
    "Role": "gpu-vm",
}

RUNNING_HEADERS = [
    "instance_id",
    "project_name",
    "created_by",
    "purchase_option",
    "instance_type",
    "launch_time_utc",
    "state",
    "last_updated_utc",
    "running_hours_estimated",
    "compute_price_usd_per_hour",
    "compute_price_eur_per_hour",
    "ebs_price_usd_per_hour",
    "ebs_price_eur_per_hour",
    "total_price_usd_per_hour",
    "total_price_eur_per_hour",
    "accumulated_cost_usd",
    "accumulated_cost_eur",
]


def _instance_tags(instance: dict[str, Any]) -> dict[str, str]:
    return {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}


def _is_managed_instance(instance: dict[str, Any]) -> bool:
    tags = _instance_tags(instance)
    return all(tags.get(key) == value for key, value in MANAGED_TAGS.items())


def _get_instance_id(event: dict[str, Any]) -> str | None:
    detail = event.get("detail", {})
    return detail.get("EC2InstanceId") or detail.get("instance-id")


def _describe_instance(instance_id: str) -> dict[str, Any]:
    reservations = EC2.describe_instances(InstanceIds=[instance_id]).get(
        "Reservations", []
    )
    if not reservations:
        raise RuntimeError(f"Instance {instance_id} was not found")
    return reservations[0]["Instances"][0]


def _list_managed_instances() -> list[dict[str, Any]]:
    paginator = EC2.get_paginator("describe_instances")
    instances: list[dict[str, Any]] = []
    for page in paginator.paginate(
        Filters=[
            {"Name": "tag:ManagedBy", "Values": [MANAGED_TAGS["ManagedBy"]]},
            {"Name": "tag:Stack", "Values": [MANAGED_TAGS["Stack"]]},
            {"Name": "tag:Role", "Values": [MANAGED_TAGS["Role"]]},
            {"Name": "instance-state-name", "Values": ["pending", "running"]},
        ]
    ):
        for reservation in page.get("Reservations", []):
            instances.extend(reservation.get("Instances", []))
    return instances


def _purchase_option(instance: dict[str, Any]) -> str:
    if instance.get("InstanceLifecycle") == "spot":
        return "spot"
    return "on-demand"


def _project_name(instance: dict[str, Any]) -> str:
    return _instance_tags(instance).get("Project", "unknown")


def _created_by(instance: dict[str, Any]) -> str:
    return _instance_tags(instance).get("CreatedBy", DEFAULT_CREATED_BY)


def _get_spot_price(instance_type: str, availability_zone: str) -> Decimal:
    response = EC2.describe_spot_price_history(
        AvailabilityZone=availability_zone,
        InstanceTypes=[instance_type],
        MaxResults=1,
        ProductDescriptions=["Linux/UNIX"],
        StartTime=datetime.now(UTC),
    )
    history = response.get("SpotPriceHistory", [])
    if not history:
        raise RuntimeError(
            f"No spot price history was returned for {instance_type} in {availability_zone}"
        )
    return Decimal(history[0]["SpotPrice"])


def _get_spot_price_at(
    instance_type: str, availability_zone: str, at_time: datetime
) -> Decimal:
    response = EC2.describe_spot_price_history(
        AvailabilityZone=availability_zone,
        EndTime=at_time,
        InstanceTypes=[instance_type],
        MaxResults=1,
        ProductDescriptions=["Linux/UNIX"],
        StartTime=at_time - timedelta(hours=6),
    )
    history = response.get("SpotPriceHistory", [])
    if not history:
        raise RuntimeError(
            f"No spot price history was returned for {instance_type} in {availability_zone} at {at_time.isoformat()}"
        )
    return Decimal(history[0]["SpotPrice"])


def _extract_on_demand_price(response: dict[str, Any]) -> Decimal:
    for price_blob in response.get("PriceList", []):
        payload = json.loads(price_blob)
        terms = payload.get("terms", {}).get("OnDemand", {})
        for term in terms.values():
            for dimension in term.get("priceDimensions", {}).values():
                if dimension.get("unit") == "Hrs":
                    return Decimal(dimension["pricePerUnit"]["USD"])
    raise RuntimeError("Unable to parse On-Demand pricing response")


def _get_on_demand_price(instance_type: str) -> Decimal:
    response = PRICING.get_products(
        ServiceCode="AmazonEC2",
        Filters=[
            {"Field": "instanceType", "Type": "TERM_MATCH", "Value": instance_type},
            {"Field": "operatingSystem", "Type": "TERM_MATCH", "Value": "Linux"},
            {"Field": "preInstalledSw", "Type": "TERM_MATCH", "Value": "NA"},
            {"Field": "regionCode", "Type": "TERM_MATCH", "Value": AWS_REGION},
            {"Field": "tenancy", "Type": "TERM_MATCH", "Value": "Shared"},
            {"Field": "capacitystatus", "Type": "TERM_MATCH", "Value": "Used"},
        ],
        MaxResults=10,
    )
    return _extract_on_demand_price(response)


def _usd_to_eur_rate() -> Decimal:
    try:
        response = SSM.get_parameter(Name=EXCHANGE_RATE_PARAMETER)
        return Decimal(response["Parameter"]["Value"])
    except Exception as exc:
        LOGGER.warning(
            "Failed to get cached exchange rate from SSM: %s. Using fallback.", exc
        )
        return Decimal("0.92")


def _to_eur(price_usd: Decimal) -> Decimal:
    usd_per_eur = _usd_to_eur_rate()
    return (price_usd / usd_per_eur).quantize(Decimal("0.0001"))


def _root_volume_size_gib(instance: dict[str, Any]) -> int:
    root_device_name = instance.get("RootDeviceName")
    for mapping in instance.get("BlockDeviceMappings", []):
        if mapping.get("DeviceName") != root_device_name:
            continue
        volume_id = mapping.get("Ebs", {}).get("VolumeId")
        if not volume_id:
            continue
        try:
            volumes = EC2.describe_volumes(VolumeIds=[volume_id]).get("Volumes", [])
            if volumes:
                return int(volumes[0]["Size"])
        except Exception as exc:
            LOGGER.warning(
                "Failed to inspect root volume %s for instance %s: %s",
                volume_id,
                instance.get("InstanceId", "unknown"),
                exc,
            )
            break
    return DEFAULT_ROOT_VOLUME_SIZE_GIB


def _get_ebs_price_per_hour(instance: dict[str, Any]) -> Decimal:
    price_per_gb_month = EBS_GP3_PRICE_PER_GB_MONTH.get(AWS_REGION, Decimal("0.092"))
    root_volume_size_gib = _root_volume_size_gib(instance)
    hourly_price = (price_per_gb_month * root_volume_size_gib) / Decimal("720")
    return hourly_price.quantize(Decimal("0.0001"))


def _load_google_service_account() -> dict[str, Any]:
    secret = SECRETS.get_secret_value(SecretId=GOOGLE_WRITER_SECRET_ARN)
    return json.loads(secret["SecretString"])


def _google_session() -> AuthorizedSession:
    credentials = service_account.Credentials.from_service_account_info(
        _load_google_service_account(),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return AuthorizedSession(credentials)


def _sheet_title(range_name: str) -> str:
    return range_name.split("!", 1)[0]


def _ensure_sheet(
    session: AuthorizedSession, range_name: str, headers: list[str]
) -> None:
    sheet_title = _sheet_title(range_name)
    metadata = session.get(
        f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_SHEETS_SPREADSHEET_ID}?fields=sheets.properties.title",
        timeout=10,
    )
    metadata.raise_for_status()
    titles = {sheet["properties"]["title"] for sheet in metadata.json().get("sheets", [])}
    if sheet_title not in titles:
        response = session.post(
            f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_SHEETS_SPREADSHEET_ID}:batchUpdate",
            json={"requests": [{"addSheet": {"properties": {"title": sheet_title}}}]},
            timeout=10,
        )
        response.raise_for_status()

    encoded_header_range = urllib.parse.quote(
        f"{sheet_title}!A1:{chr(64 + len(headers))}1", safe=""
    )
    response = session.put(
        f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_SHEETS_SPREADSHEET_ID}/values/{encoded_header_range}?valueInputOption=RAW",
        json={"majorDimension": "ROWS", "values": [headers]},
        timeout=10,
    )
    response.raise_for_status()


def _append_to_sheet(
    session: AuthorizedSession, range_name: str, row: list[Any]
) -> None:
    encoded_range = urllib.parse.quote(range_name, safe="")
    response = session.post(
        f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_SHEETS_SPREADSHEET_ID}/values/{encoded_range}:append?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS",
        json={"values": [row]},
        timeout=10,
    )
    response.raise_for_status()


def _read_sheet(session: AuthorizedSession, range_name: str) -> list[list[str]]:
    encoded_range = urllib.parse.quote(range_name, safe="")
    response = session.get(
        f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_SHEETS_SPREADSHEET_ID}/values/{encoded_range}",
        timeout=10,
    )
    response.raise_for_status()
    return response.json().get("values", [])


def _row_lookup(rows: list[list[str]]) -> dict[str, int]:
    return {row[0]: index for index, row in enumerate(rows[1:], start=2) if row}


def _normalize_existing_row(existing_row: list[str]) -> list[str]:
    if len(existing_row) >= len(RUNNING_HEADERS):
        return existing_row
    if len(existing_row) >= 3 and existing_row[2] in {"spot", "on-demand"}:
        existing_row = [existing_row[0], existing_row[1], "", *existing_row[2:]]
    if len(existing_row) < len(RUNNING_HEADERS):
        existing_row = existing_row + [""] * (len(RUNNING_HEADERS) - len(existing_row))
    return existing_row


def _update_row(
    session: AuthorizedSession, sheet_title: str, row_number: int, row: list[Any]
) -> None:
    end_column = chr(64 + len(row))
    encoded_range = urllib.parse.quote(
        f"{sheet_title}!A{row_number}:{end_column}{row_number}", safe=""
    )
    response = session.put(
        f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_SHEETS_SPREADSHEET_ID}/values/{encoded_range}?valueInputOption=USER_ENTERED",
        json={"majorDimension": "ROWS", "values": [row]},
        timeout=10,
    )
    response.raise_for_status()


def _format_decimal(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.0001")))


def _price_for_running_cost(
    instance: dict[str, Any], purchase_option: str
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal, Decimal]:
    launch_time = instance["LaunchTime"].astimezone(UTC)
    compute_price_usd = (
        _get_spot_price_at(
            instance["InstanceType"],
            instance["Placement"]["AvailabilityZone"],
            launch_time,
        )
        if purchase_option == "spot"
        else _get_on_demand_price(instance["InstanceType"])
    )
    compute_price_eur = _to_eur(compute_price_usd)
    ebs_price_usd = _get_ebs_price_per_hour(instance)
    ebs_price_eur = _to_eur(ebs_price_usd)
    total_price_usd = compute_price_usd + ebs_price_usd
    total_price_eur = compute_price_eur + ebs_price_eur
    return compute_price_usd, compute_price_eur, ebs_price_usd, ebs_price_eur, total_price_usd, total_price_eur


def _running_cost_row(
    instance: dict[str, Any],
    compute_price_usd: Decimal,
    compute_price_eur: Decimal,
    ebs_price_usd: Decimal,
    ebs_price_eur: Decimal,
    total_price_usd: Decimal,
    total_price_eur: Decimal,
    purchase_option: str,
) -> list[str]:
    now = datetime.now(UTC)
    launch_time = instance["LaunchTime"].astimezone(UTC)
    running_hours = Decimal(str((now - launch_time).total_seconds() / 3600)).quantize(Decimal("0.0001"))
    accumulated_usd = (total_price_usd * running_hours).quantize(Decimal("0.0001"))
    accumulated_eur = (total_price_eur * running_hours).quantize(Decimal("0.0001"))
    return [
        instance["InstanceId"],
        _project_name(instance),
        _created_by(instance),
        purchase_option,
        instance["InstanceType"],
        launch_time.isoformat(),
        instance["State"]["Name"],
        now.isoformat(),
        _format_decimal(running_hours),
        _format_decimal(compute_price_usd),
        _format_decimal(compute_price_eur),
        _format_decimal(ebs_price_usd),
        _format_decimal(ebs_price_eur),
        _format_decimal(total_price_usd),
        _format_decimal(total_price_eur),
        _format_decimal(accumulated_usd),
        _format_decimal(accumulated_eur),
    ]


def _finalized_running_cost_row(existing_row: list[str], state: str) -> list[str]:
    now = datetime.now(UTC)
    existing_row = _normalize_existing_row(existing_row)
    launch_time = datetime.fromisoformat(existing_row[5])
    total_price_usd = Decimal(existing_row[13])
    total_price_eur = Decimal(existing_row[14])
    running_hours = Decimal(str((now - launch_time).total_seconds() / 3600)).quantize(Decimal("0.0001"))
    accumulated_usd = (total_price_usd * running_hours).quantize(Decimal("0.0001"))
    accumulated_eur = (total_price_eur * running_hours).quantize(Decimal("0.0001"))
    return [
        existing_row[0],
        existing_row[1],
        existing_row[2],
        existing_row[3],
        existing_row[4],
        existing_row[5],
        state,
        now.isoformat(),
        _format_decimal(running_hours),
        existing_row[9],
        existing_row[10],
        existing_row[11],
        existing_row[12],
        existing_row[13],
        existing_row[14],
        _format_decimal(accumulated_usd),
        _format_decimal(accumulated_eur),
    ]


def _upsert_running_cost_row(
    session: AuthorizedSession,
    instance: dict[str, Any],
    state_override: str | None = None,
) -> str:
    _ensure_sheet(session, RUNNING_COSTS_SHEET_RANGE, RUNNING_HEADERS)
    rows = _read_sheet(session, RUNNING_COSTS_SHEET_RANGE)
    lookup = _row_lookup(rows)
    purchase_option = _purchase_option(instance)
    compute_usd, compute_eur, ebs_usd, ebs_eur, total_usd, total_eur = _price_for_running_cost(instance, purchase_option)
    row = _running_cost_row(instance, compute_usd, compute_eur, ebs_usd, ebs_eur, total_usd, total_eur, purchase_option)
    if state_override is not None:
        row[6] = state_override

    row_number = lookup.get(instance["InstanceId"])
    if row_number is None:
        _append_to_sheet(session, RUNNING_COSTS_SHEET_RANGE, row)
        return "appended"

    _update_row(session, _sheet_title(RUNNING_COSTS_SHEET_RANGE), row_number, row)
    return "updated"


def _finalize_running_cost_row(
    session: AuthorizedSession, instance_id: str, state: str
) -> dict[str, Any]:
    _ensure_sheet(session, RUNNING_COSTS_SHEET_RANGE, RUNNING_HEADERS)
    rows = _read_sheet(session, RUNNING_COSTS_SHEET_RANGE)
    lookup = _row_lookup(rows)
    row_number = lookup.get(instance_id)
    if row_number is None:
        return {"status": "ignored", "reason": "running-cost-row-not-found", "instance_id": instance_id}

    existing_row = _normalize_existing_row(rows[row_number - 1])
    finalized_row = _finalized_running_cost_row(existing_row, state)
    _update_row(session, _sheet_title(RUNNING_COSTS_SHEET_RANGE), row_number, finalized_row)
    return {"status": "running-cost-finalized", "instance_id": instance_id, "state": state}


def _upsert_running_costs(session: AuthorizedSession) -> dict[str, Any]:
    _ensure_sheet(session, RUNNING_COSTS_SHEET_RANGE, RUNNING_HEADERS)
    active_instances = _list_managed_instances()
    updated = 0
    appended = 0
    for instance in active_instances:
        result = _upsert_running_cost_row(session, instance)
        if result == "appended":
            appended += 1
        else:
            updated += 1
    return {"status": "running-costs-updated", "updated": updated, "appended": appended, "instances_seen": len(active_instances)}


def _handle_launch_success(
    event: dict[str, Any], session: AuthorizedSession
) -> dict[str, Any]:
    instance_id = _get_instance_id(event)
    if not instance_id:
        return {"status": "ignored", "reason": "missing-instance-id"}

    instance = _describe_instance(instance_id)
    if not _is_managed_instance(instance):
        return {"status": "ignored", "reason": "instance-not-managed-by-this-stack"}

    purchase_option = _purchase_option(instance)
    compute_usd, compute_eur, ebs_usd, ebs_eur, total_usd, total_eur = _price_for_running_cost(instance, purchase_option)
    running_row_result = _upsert_running_cost_row(session, instance)
    return {
        "status": "running-cost-row-synced",
        "instance_id": instance_id,
        "purchase_option": purchase_option,
        "compute_price_usd_per_hour": _format_decimal(compute_usd),
        "compute_price_eur_per_hour": _format_decimal(compute_eur),
        "ebs_price_usd_per_hour": _format_decimal(ebs_usd),
        "ebs_price_eur_per_hour": _format_decimal(ebs_eur),
        "total_price_usd_per_hour": _format_decimal(total_usd),
        "total_price_eur_per_hour": _format_decimal(total_eur),
        "running_cost_row": running_row_result,
    }


def _handle_instance_terminated(
    event: dict[str, Any], session: AuthorizedSession
) -> dict[str, Any]:
    detail = event.get("detail", {})
    if detail.get("state") != "terminated":
        return {"status": "ignored", "reason": "unsupported-instance-state"}

    instance_id = _get_instance_id(event)
    if not instance_id:
        return {"status": "ignored", "reason": "missing-instance-id"}

    return _finalize_running_cost_row(session, instance_id, state="terminated")


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    LOGGER.info("Received event: %s", json.dumps(event))
    session = _google_session()
    detail_type = event.get("detail-type")
    source = event.get("source")

    if source == "aws.autoscaling" and detail_type == "EC2 Instance Launch Successful":
        return _handle_launch_success(event, session)

    if source == "aws.events" and detail_type == "Scheduled Event":
        return _upsert_running_costs(session)

    if source == "aws.ec2" and detail_type == "EC2 Instance State-change Notification":
        return _handle_instance_terminated(event, session)

    return {"status": "ignored", "reason": "unsupported-event"}
