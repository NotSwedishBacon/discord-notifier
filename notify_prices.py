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
import matplotlib.cm as cm

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
    recommendation_points = [point for point in price_points if point[0].hour >= 6]
    recommendation_average = mean(price for _, price in recommendation_points)
    recommendation_minimum = min(price for _, price in recommendation_points)
    recommendation_maximum = max(price for _, price in recommendation_points)
    cheap_threshold = recommendation_minimum + (
        recommendation_average - recommendation_minimum
    ) * 0.50
    expensive_threshold = recommendation_maximum - (
        recommendation_maximum - recommendation_average
    ) * 0.65
    cheapest_window = find_price_window(
        recommendation_points,
        cheap_threshold,
        cheapest=True,
    )
    most_expensive_window = find_price_window(
        recommendation_points,
        expensive_threshold,
        cheapest=False,
    )
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


def find_price_window(
    price_points: list[tuple[datetime, float]],
    average: float,
    *,
    cheapest: bool,
) -> tuple[datetime, datetime, float]:
    windows: list[list[tuple[datetime, float]]] = []
    current: list[tuple[datetime, float]] = []
    for point in price_points:
        is_match = point[1] <= average if cheapest else point[1] >= average
        is_contiguous = current and point[0] - current[-1][0] == timedelta(minutes=15)
        if is_match and (not current or is_contiguous):
            current.append(point)
        else:
            if current:
                windows.append(current)
            current = [point] if is_match else []
    if current:
        windows.append(current)

    selected = (
        min(windows, key=lambda window: mean(price for _, price in window))
        if cheapest
        else max(windows, key=lambda window: mean(price for _, price in window))
    )
    return (
        selected[0][0],
        selected[-1][0] + timedelta(minutes=15),
        mean(price for _, price in selected),
    )


def create_chart(
    price_points: list[tuple[datetime, float]],
    price_class: str,
    date: datetime,
) -> bytes:
    hourly: dict[datetime, list[float]] = {}
    for time, price in price_points:
        hour = time.replace(minute=0, second=0, microsecond=0)
        hourly.setdefault(hour, []).append(price)

    hourly_points = sorted((hour, mean(prices)) for hour, prices in hourly.items())
    values = [price for _, price in hourly_points]
    minimum = min(values)
    maximum = max(values)
    spread = maximum - minimum
    colormap_registry = getattr(matplotlib, "colormaps", None)
    colormap = (
        colormap_registry["RdYlGn_r"]
        if colormap_registry is not None
        else cm.get_cmap("RdYlGn_r")
    )
    colors = [colormap((price - minimum) / spread if spread else 0.5) for price in values]

    figure, axis = plt.subplots(figsize=(12, 6.5), dpi=150, facecolor="#202225")
    axis.set_facecolor("#202225")
    hours = list(range(1, len(hourly_points) + 1))
    bars = axis.bar(hours, values, color=colors, width=0.78, edgecolor="#151619", linewidth=0.5)
    axis.set_title(
        f"DAGLIGA ELPRISER {date:%Y-%m-%d}",
        color="white",
        fontsize=16,
        fontweight="bold",
        loc="center",
        pad=18,
    )
    axis.set_ylabel("SPOTPRIS (SEK/KWH)", color="#d7d9dc", fontsize=10, fontweight="bold")
    axis.set_xlabel("TIMME", color="#d7d9dc", fontsize=10, fontweight="bold", labelpad=10)
    axis.set_xticks(hours)
    axis.set_xticklabels([str(hour) for hour in hours], color="#d7d9dc")
    axis.tick_params(axis="y", colors="#d7d9dc")
    axis.grid(axis="y", color="#4b4d52", alpha=0.45, linewidth=0.7)
    axis.set_axisbelow(True)
    for spine in axis.spines.values():
        spine.set_visible(False)
    axis.set_ylim(0, max(maximum * 1.18, 1))
    for bar, value in zip(bars, values):
        axis.text(
            bar.get_x() + bar.get_width() / 2,
            value + maximum * 0.025,
            f"{value:.2f}",
            ha="center",
            va="bottom",
            color="#d7d9dc",
            fontsize=7,
        )
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
        summary, _, _, _, price_points = format_prices(prices)
        send_to_discord(
            webhook_url,
            price_class,
            today,
            summary,
            create_chart(price_points, price_class, today),
        )
        print(f"Posted {len(prices)} price points for {today:%Y-%m-%d} ({price_class})")
        return 0
    except (HTTPError, URLError, RuntimeError) as error:
        print(f"Notification failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())