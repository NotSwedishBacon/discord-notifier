# discord-notifier

Posts the day's Swedish electricity prices to a Discord channel using GitHub Actions.

## Setup

1. Create a Discord webhook for the channel that should receive the notification.
2. Add these **repository secrets** under `Settings` -> `Secrets and variables` -> `Actions`:
	- `DISCORD_WEBHOOK_URL`: the complete Discord webhook URL.
	- `PRISKLASS`: one of `SE1`, `SE2`, `SE3`, or `SE4`.
3. Enable Actions for the repository.

The workflow runs at 18:00 for tomorrow's prices. It can also be started manually from the Actions tab. The script aggregates the API's 15-minute prices into hourly averages, and attaches a chart where cheaper hours are green and more expensive hours are red.

The webhook URL and price area are read only from GitHub Secrets; neither value is stored in this repository.
