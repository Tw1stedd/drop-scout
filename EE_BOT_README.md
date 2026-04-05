# Entertainment Earth Checkout Bot

Fast, accurate, and efficient automated checkout bot for Entertainment Earth using Playwright.

---

## Quick Start

1. **Edit `ee_config.json`** — fill in your account, targets, and billing info
2. **Double-click `START_EE_BOT.bat`** — auto-installs deps and launches the bot

---

## Setup (Manual)

```bash
pip install -r ee_requirements.txt
playwright install chromium
python ee_bot.py
```

---

## Configuration (`ee_config.json`)

### Account
```json
"account": {
  "email": "your@email.com",
  "password": "yourpassword"
}
```

### Targets — add as many as you want
```json
"targets": [
  {
    "name": "Star Wars Figure",
    "url": "https://www.entertainmentearth.com/product/...",
    "item_number": "SW12345",
    "max_price": 35.00,
    "quantity": 1,
    "enabled": true
  }
]
```
- `url` — full product page URL
- `item_number` — EE item code (used if URL not set)
- `max_price` — skip if price exceeds this (0 = no limit)
- `enabled` — set to `false` to skip a target

### Checkout
```json
"checkout": {
  "shipping": { ... },
  "payment": { ... },
  "use_saved_payment": true,
  "shipping_method": "standard"
}
```
- Set `use_saved_payment: true` to use your EE saved card (fastest)
- `shipping_method`: `"standard"`, `"ground"`, or `"expedited"`

### Bot Settings
| Key | Default | Description |
|-----|---------|-------------|
| `monitor_interval_seconds` | 5 | Seconds between availability checks |
| `headless` | false | Run browser invisibly |
| `auto_checkout` | true | Set false to fill cart but NOT place order |
| `save_session` | true | Cache login cookies for faster restarts |
| `discord_webhook_url` | "" | Post success/fail notifications to Discord |
| `proxy` | "" | Optional proxy (`http://ip:port`) |

---

## CLI Arguments

```bash
# Monitor all targets from config
python ee_bot.py

# Quick checkout a single URL
python ee_bot.py --url "https://www.entertainmentearth.com/product/..."

# Fill cart but don't place order (safe testing)
python ee_bot.py --no-auto-checkout

# Run headless
python ee_bot.py --headless

# Monitor only (check availability, no checkout)
python ee_bot.py --monitor-only
```

---

## Features

- **Session caching** — stays logged in between runs (no repeated logins)
- **Stealth mode** — removes browser automation fingerprints
- **Price guard** — skips if item price exceeds your max
- **Multi-target** — monitors and checks out multiple items
- **Discord alerts** — notifies you on success or failure
- **Iframe payment** — handles Stripe/Braintree card iframes
- **Popup dismissal** — auto-closes cookie banners and modals
- **Retry resilient** — multiple selector fallbacks for every action

---

## Notes

- Set `auto_checkout: false` first to test the flow without placing a real order
- Saved session (`ee_session.json`) is created automatically after first login
- Delete `ee_session.json` to force a fresh login
