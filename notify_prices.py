#!/usr/bin/env python3
"""Post today's Swedish electricity prices to a Discord webhook."""

import json
import os
import re
import sys
from io import BytesIO
from datetime import datetime, timedelta
from statistics import mean
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


API_BASE_URL = "https://www.elprisetjustnu.se/api/v1/prices"
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


def format_prices(
    prices: list[dict],
) -> tuple[str, float, float, float, list[tuple[datetime, float]]]:
    price_points: list[tuple[datetime, float]] = []
    for item in prices:
        try:
            start = datetime.fromisoformat(item["time_start"])
            price = float(item["SEK_per_kWh"])
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("The electricity price API returned invalid data") from error
        price_points.append((start, price))

    price_points.sort()
    all_prices = [price for _, price in price_points]
    average = mean(all_prices)
    cheapest_window = find_ranked_window(price_points, cheapest=True)
    most_expensive_window = find_ranked_window(price_points, cheapest=False)
    summary = (
        f"**Prisöversikt**\n"
        f"Min: {min(all_prices):.2f} SEK/kWh\n"
        f"Max: {max(all_prices):.2f} SEK/kWh\n"
        f"Snitt: {average:.2f} SEK/kWh\n\n"
        f"**Billigast**\n"
        f"{cheapest_window[0]:%H:%M}-{cheapest_window[1]:%H:%M} "
        f"({cheapest_window[2]:.2f} SEK/kWh i snitt)\n\n"
        f"**Dyrast**\n"
        f"{most_expensive_window[0]:%H:%M}-{most_expensive_window[1]:%H:%M} "
        f"({most_expensive_window[2]:.2f} SEK/kWh i snitt)"
    )

    return (
        summary,
        min(all_prices),
        max(all_prices),
        mean(all_prices),
        price_points,
    )


def find_ranked_window(
    price_points: list[tuple[datetime, float]],
    *,
    cheapest: bool,
) -> tuple[datetime, datetime, float]:
    average_price = mean(price for _, price in price_points)
    recommendation_points = [point for point in price_points if point[0].hour >= 6]
    windows: list[list[tuple[datetime, float]]] = []
    current: list[tuple[datetime, float]] = []
    for point in recommendation_points:
        is_cheap = point[1] < average_price
        is_match = is_cheap if cheapest else not is_cheap
        is_contiguous = current and point[0] - current[-1][0] == timedelta(minutes=15)
        if is_match and (not current or is_contiguous):
            current.append(point)
        else:
            if current:
                windows.append(current)
            current = [point] if is_match else []
    if current:
        windows.append(current)

    def window_score(window: list[tuple[datetime, float]]) -> float:
        distances = [
            (average_price - price if cheapest else price - average_price)
            for _, price in window
        ]
        return sum(distances) + max(distances)

    selected = max(windows, key=window_score)
    return (
        selected[0][0],
        selected[-1][0] + timedelta(minutes=15),
        mean(price for _, price in selected),
    )


def create_chart(
    price_points: list[tuple[datetime, float]],
    date: datetime,
) -> bytes:
    interval_points = sorted(price_points)
    values = [price for _, price in interval_points]
    maximum = max(values)
    average = mean(values)
    colors = ["#43a047" if price < average else "#e53935" for price in values]

    figure, axis = plt.subplots(figsize=(12, 6.5), dpi=150, facecolor="#202225")
    axis.set_facecolor("#202225")
    intervals = list(range(1, len(interval_points) + 1))
    for start in range(1, len(interval_points) + 1, 8):
        axis.axvspan(
            start - 0.5,
            min(start + 3.5, len(interval_points) + 0.5),
            facecolor="#2a2d31",
            zorder=0,
        )
    bars = axis.bar(
        intervals,
        values,
        color=colors,
        width=0.9,
        edgecolor="#151619",
        linewidth=0.5,
    )
    axis.set_title(
        f"DAGLIGA ELPRISER {date:%Y-%m-%d}",
        color="white",
        fontsize=16,
        fontweight="bold",
        loc="center",
        pad=18,
    )
    axis.set_ylabel("SPOTPRIS (SEK/KWH)", color="#d7d9dc", fontsize=10, fontweight="bold")
    axis.set_xlabel("TID", color="#d7d9dc", fontsize=10, fontweight="bold", labelpad=10)
    hourly_intervals = [
        index for index, (time, _) in enumerate(interval_points, start=1)
        if time.minute == 0
    ]
    axis.set_xticks([index + 1.5 for index in hourly_intervals])
    axis.set_xticklabels(
        [interval_points[index - 1][0].strftime("%H") for index in hourly_intervals],
        color="#d7d9dc",
    )
    axis.tick_params(axis="both", length=0)
    axis.tick_params(axis="y", colors="#d7d9dc")
    axis.grid(axis="y", color="#4b4d52", alpha=0.45, linewidth=0.7)
    axis.set_axisbelow(True)
    for spine in axis.spines.values():
        spine.set_visible(False)
    axis.set_ylim(0, max(maximum * 1.18, 1))
    figure.tight_layout(pad=1.5)

    image = BytesIO()
    figure.savefig(image, format="png", facecolor="#202225")
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
                "description": content,
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
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "discord-notifier/1.0",
        },
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

def main() -> int:
    try:
        webhook_url = get_required_environment("DISCORD_WEBHOOK_URL")
        price_class = get_required_environment("PRISKLASS").upper()
        if not PRICE_CLASS_PATTERN.fullmatch(price_class):
            raise RuntimeError("PRISKLASS must be one of SE1, SE2, SE3, or SE4")

        target_date = datetime.now() + timedelta(days=1)
        prices = fetch_prices(price_class, target_date)
        summary, _, _, _, price_points = format_prices(prices)
        send_to_discord(
            webhook_url,
            price_class,
            target_date,
            summary,
            create_chart(price_points, target_date),
        )
        print(f"Posted {len(prices)} price points for {target_date:%Y-%m-%d} ({price_class})")
        return 0
    except (HTTPError, URLError, RuntimeError) as error:
        print(f"Notification failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())