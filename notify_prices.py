#!/usr/bin/env python3
"""Post today's Swedish electricity prices to a Discord webhook."""

import json
import os
import re
import sys
from io import BytesIO
from datetime import datetime
from statistics import mean
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import matplotlib
from matplotlib.cm import get_cmap

matplotlib.use("Agg")
import matplotlib.pyplot as plt


API_BASE_URL = "https://www.elprisetjustnu.se/api/v1/prices"
STOCKHOLM = ZoneInfo("Europe/Stockholm")
PRICE_CLASS_PATTERN = re.compile(r"^SE[1-4]$")


def get_required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def fetch_prices(price_class: str, date: datetime) -> list[dict]:
    url = f"{API_BASE_URL}/{date:%Y/%m-%d}_{price_class}.json"
    request = Request(url, headers={"User-Agent": "discord-notifier/1.0"})
    try:
        with urlopen(request, timeout=30) as response:
            prices = json.load(response)
    except HTTPError as error:
        raise RuntimeError(f"Electricity price API returned HTTP {error.code}") from error

    if not isinstance(prices, list) or not prices:
        raise RuntimeError("The electricity price API returned no prices")
    return prices


def format_hourly_prices(
    prices: list[dict],
) -> tuple[str, float, float, float, list[tuple[str, float]]]:
    hourly: dict[str, list[float]] = {}
    for item in prices:
        try:
            start = datetime.fromisoformat(item["time_start"])
            price = float(item["SEK_per_kWh"])
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("The electricity price API returned invalid data") from error
        hour = start.replace(minute=0, second=0, microsecond=0)
        hourly.setdefault(hour.strftime("%H:%M"), []).append(price)

    hourly_prices = [(hour, mean(values)) for hour, values in hourly.items()]
    rows = []
    all_prices = [price for values in hourly.values() for price in values]
    for hour, price in hourly_prices:
        rows.append(f"{hour}  {price:.2f} kr/kWh")

    return (
        "\n".join(rows),
        min(all_prices),
        max(all_prices),
        mean(all_prices),
        hourly_prices,
    )


def create_chart(hourly_prices: list[tuple[str, float]]) -> bytes:
    labels = [hour for hour, _ in hourly_prices]
    values = [price for _, price in hourly_prices]
    minimum = min(values)
    maximum = max(values)
    spread = maximum - minimum
    colormap = get_cmap("RdYlGn_r")
    colors = [
        colormap((price - minimum) / spread if spread else 0.5)
        for price in values
    ]

    figure, axis = plt.subplots(figsize=(12, 5), dpi=150)
    axis.bar(labels, values, color=colors, edgecolor="#333333", linewidth=0.3)
    axis.set_title("Elpris per timme")
    axis.set_ylabel("SEK/kWh")
    axis.grid(axis="y", alpha=0.25)
    axis.set_axisbelow(True)
    axis.tick_params(axis="x", rotation=45)
    figure.tight_layout()

    image = BytesIO()
    figure.savefig(image, format="png", facecolor="white")
    plt.close(figure)
    return image.getvalue()


def send_to_discord(
    webhook_url: str,
    price_class: str,
    date: datetime,
    content: str,
    chart: bytes,
) -> None:
    boundary = "----discord-notifier-boundary"
    payload = {
        "username": "Elpris",
        "embeds": [
            {
                "title": f"Elpriser {date:%Y-%m-%d} ({price_class})",
                "description": f"```text\n{content}\n```",
                "color": 0x116530,
                "footer": {"text": "Källa: elprisetjustnu.se"},
                "image": {"url": "attachment://prices.png"},
            }
        ],
    }
    body = (
        f"--{boundary}\r\n"
        "Content-Disposition: form-data; name=\"payload_json\"\r\n"
        "Content-Type: application/json\r\n\r\n"
        f"{json.dumps(payload)}\r\n"
        f"--{boundary}\r\n"
        "Content-Disposition: form-data; name=\"files[0]\"; filename=\"prices.png\"\r\n"
        "Content-Type: image/png\r\n\r\n"
    ).encode("utf-8") + chart + f"\r\n--{boundary}--\r\n".encode("utf-8")
    request = Request(
        webhook_url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            if response.status not in (200, 204):
                raise RuntimeError(f"Discord returned HTTP {response.status}")
    except HTTPError as error:
        response_body = error.read().decode("utf-8", errors="replace").strip()
        details = f": {response_body}" if response_body else ""
        raise RuntimeError(
            f"Discord webhook returned HTTP {error.code}{details}"
        ) from error


def should_run_now() -> bool:
    return os.environ.get("GITHUB_EVENT_NAME") != "schedule" or datetime.now(STOCKHOLM).hour == 5


def main() -> int:
    try:
        if not should_run_now():
            print("Skipping UTC trigger outside 05:00 Europe/Stockholm")
            return 0

        webhook_url = get_required_environment("DISCORD_WEBHOOK_URL")
        price_class = get_required_environment("PRISKLASS").upper()
        if not PRICE_CLASS_PATTERN.fullmatch(price_class):
            raise RuntimeError("PRISKLASS must be one of SE1, SE2, SE3, or SE4")

        today = datetime.now(STOCKHOLM)
        prices = fetch_prices(price_class, today)
        hourly, minimum, maximum, average, hourly_prices = format_hourly_prices(prices)
        summary = (
            f"Min: {minimum:.2f} kr/kWh | Max: {maximum:.2f} kr/kWh | "
            f"Snitt: {average:.2f} kr/kWh\n\n{hourly}"
        )
        send_to_discord(
            webhook_url,
            price_class,
            today,
            summary,
            create_chart(hourly_prices),
        )
        print(f"Posted {len(prices)} price points for {today:%Y-%m-%d} ({price_class})")
        return 0
    except (HTTPError, URLError, RuntimeError) as error:
        print(f"Notification failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())