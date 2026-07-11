# PS5 India stock checker

This project checks PS5 Disc and Digital console availability for delivery to
pincode **575006** and sends a Telegram alert when stock appears.

Configured retailers:

- Amazon India
- Flipkart
- Sony ShopAtSC
- Croma
- Reliance Digital
- Vijay Sales
- Games The Shop

It uses Playwright with headless Chromium because several retailer pages render
availability through JavaScript. It does not log in, solve CAPTCHAs, bypass
anti-bot protections, or place an order.

## Cheapest deployment: GitHub Actions

Use a **public GitHub repository**. Standard GitHub-hosted Actions usage is free
for public repositories. Your Telegram credentials remain encrypted in GitHub
Actions secrets and are not stored in this project.

The workflow checks at minutes 7, 22, 37 and 52 of every hour, roughly every
15 minutes. Scheduled GitHub Actions can occasionally start late. GitHub may
automatically disable a public repository's scheduled workflow after 60 days
without repository activity, so re-enable it if monitoring lasts that long.

## Setup

1. Create a new public GitHub repository, for example `ps5-stock-checker`.
2. Upload every file and folder from this project, including `.github`.
3. Open the repository's **Settings**.
4. Go to **Secrets and variables → Actions**.
5. Create these repository secrets:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
6. Open the **Actions** tab and enable workflows when GitHub asks.
7. Open **Check PS5 stock** and select **Run workflow**.
8. Enable **Send a Telegram test message before checking** on the first manual
   run to verify the secrets.

Your existing Telegram bot token and chat ID can be reused.

## How duplicate alerts are prevented

`state.json` records only the last known availability status and the last
product URL that generated an alert. The workflow commits that file only when
the status changes, so it does not create a commit every 15 minutes.

An `UNKNOWN` result, such as a CAPTCHA or page timeout, never overwrites a known
stock state and never triggers a false stock alert.

## Adjust the delivery pincode

Edit this value in `.github/workflows/check-stock.yml`:

```yaml
DELIVERY_PINCODE: "575006"
```

## Add, remove or disable products

Edit `products.json`.

To temporarily disable one entry:

```json
"enabled": false
```

Product entries use direct pages. The Flipkart entry uses a search page because
Flipkart product URLs often change. It only accepts listings containing PS5
console terms and a price between the configured minimum and maximum, while
excluding accessories.

## Change the frequency

The included cron expression runs four times per hour:

```yaml
- cron: "7,22,37,52 * * * *"
```

For every 30 minutes:

```yaml
- cron: "7,37 * * * *"
```

GitHub's shortest supported scheduled interval is five minutes, but a
five-minute browser check creates substantially more traffic and is unnecessary
for normal monitoring.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium

export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
export DELIVERY_PINCODE="575006"
python stock_checker.py
```

On Windows PowerShell:

```powershell
$env:TELEGRAM_BOT_TOKEN="..."
$env:TELEGRAM_CHAT_ID="..."
$env:DELIVERY_PINCODE="575006"
python stock_checker.py
```

## Important limitations

Retailers can redesign pages, remove listings, show different inventory after
login, or block automated browsers. The checker deliberately reports those
cases as `UNKNOWN` rather than guessing.

A stock notification means the site exposed a strong availability signal, but
inventory can disappear before checkout. Open the link immediately and confirm
the seller, warranty, delivery pincode and final price.

Check retailer terms and keep the interval reasonable. Do not reduce the delay
between websites or use the code to bypass access controls.

## Stop the checker after buying

Open the repository's **Actions** tab, select **Check PS5 stock**, open the
three-dot menu and choose **Disable workflow**.
