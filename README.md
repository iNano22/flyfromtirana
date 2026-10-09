# FlyFromTirana ✈️

Scans cheap flights from Tirana (TIA), spots deals, and posts them to the
Telegram channel [@flyfromtirana](https://t.me/flyfromtirana) with affiliate
links. It runs every 3 hours on GitHub Actions, so there's no server to manage.

## How it works

```
GitHub Actions (every 3h)
  └─ python -m src.main
       1. scanner.py   fetch prices TIA → each destination (and back) for the next 60 days
       2. storage.py   save them in data/prices.db (SQLite)
       3. deals.py     deal = ≥40% below the route's 30-day median, or under the route's € threshold
                       skip anything already posted in the last 7 days at the same price band
       4. formatter.py build the Albanian post from templates/sq.txt (links from links.py)
       5. telegram.py  send it to the channel (best 3 deals per run)
  └─ commit data/prices.db back to the repo, so price history persists
```

Prices come from the Travelpayouts **Data API** (`aviasales/v3/prices_for_dates`).
These are *cached* prices that Aviasales users found recently, not live
availability. That's fine for spotting deals: the booking link opens a live search.

## Project layout

```
config.yaml              routes, deal rules, link templates (no secrets)
templates/sq.txt         post wording (Albanian)
src/
  main.py                orchestrator + CLI (--dry-run, --routes)
  config.py              loads config.yaml + env vars
  scanner.py             Travelpayouts API client
  storage.py             SQLite: price history + posted deals
  deals.py               deal detection, dedupe, ranking
  links.py               ALL affiliate link building
  formatter.py           Deal → post text
  telegram.py            Telegram Bot API client
  http_client.py         shared retry/backoff for HTTP calls
scripts/post_test.py     send one test message to the channel
tests/                   pytest suite (HTTP is mocked; no network needed)
.github/workflows/       scan.yml (every 3h), tests.yml (on push)
```

## Setup

### 1. Travelpayouts (prices + affiliate marker)

1. Sign up at [travelpayouts.com](https://www.travelpayouts.com) and add a project
   (your Telegram channel).
2. Join the **Aviasales** program (Programs → Aviasales → Join).
3. Copy your **API token** (dashboard → API / Tools section) → `TRAVELPAYOUTS_TOKEN`.
4. Copy your **marker** (your partner ID, a number shown in your profile) →
   `TRAVELPAYOUTS_MARKER`.
5. *(Optional)* Note your **project ID** if you want to wrap links in the
   `tp.media` tracking redirect (see `links.flight.wrapper` in config.yaml).

### 2. Telegram bot

1. In Telegram, talk to [@BotFather](https://t.me/BotFather) → `/newbot` → copy the
   token → `TELEGRAM_BOT_TOKEN`.
2. Open **@flyfromtirana** → Manage channel → Administrators → Add admin → pick
   your bot → enable **Post messages**.
3. `TELEGRAM_CHANNEL_ID` = `@flyfromtirana`.

### 3. Run locally

Needs Python 3.11 (`brew install python@3.11`, or `uv python install 3.11`).

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill in your 4 values
```

```bash
python -m src.main --dry-run --routes BGY,VIE   # a quick look at 2 routes
python -m src.main --dry-run                    # all routes, prints posts, sends nothing
python scripts/post_test.py                     # send one test message to the channel
python -m pytest                                # run the tests
python -m src.main                              # real run: posts to the channel
```

A dry run still saves prices to `data/prices.db` (that's what builds the
median), but it never marks deals as posted.

### 4. GitHub Actions

1. Create a repo named `flyfromtirana` and push this folder.
2. Settings → Secrets and variables → Actions → **New repository secret**, add:
   `TRAVELPAYOUTS_TOKEN`, `TRAVELPAYOUTS_MARKER`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`.
3. Actions tab → **Scan flights** → **Run workflow** with "Dry run" ticked →
   check the log shows sensible posts.
4. That's it. It now runs every 3 hours and commits `data/prices.db` after each run.

If the commit step fails with a 403: Settings → Actions → General → Workflow
permissions → **Read and write permissions**.

## Configuration (config.yaml)

### Deal rules

| Key | Default | Meaning |
|---|---|---|
| `deal_discount_pct` | 40 | Deal if the price is ≥40% below the route's median (price ≤ 60% of median) |
| `median_window_days` | 30 | Median is over every price seen for the route in this window |
| `min_samples_for_median` | 10 | Median isn't trusted until we have this many prices |
| `absolute_threshold_eur` (per route) | | A price at/below this is a deal, if it's also ≥ `threshold_min_discount_pct` (25%) below the median |
| `repost_cooldown_days` | 7 | Same route + date + price band isn't reposted within this window |
| `price_band_eur` | 10 | €20–29.99 is one band. A drop into a cheaper band *is* reposted |
| `max_posts_per_run` | 2 | Best deals (biggest % saving) go first |
| `quiet_hours` | 23:00–08:00 | Scan at night but don't post. `null` to disable |

One post covers one route. It shows the cheapest date plus up to 3 more dates
within 10% of that price, and the cheapest flight back 2–14 days later.

On the very first run there's no history yet. The median then comes from
that run's own prices across ~60 dates, so deals can still be found right away.

### Add a route

Add one line under `routes:`:

```yaml
  - { iata: PRG, city: Pragë, city_en: Prague, flag: "🇨🇿", absolute_threshold_eur: 35 }
```

Use `airport:` for cities with several airports (it's shown as "MILANO (Bergamo)").
Try it with `python -m src.main --dry-run --routes PRG`.

### Change the post wording

Edit `templates/sq.txt`. Rules:

- `{NAME}` is replaced with a value: `CITY CITY_UPPER AIRPORT FLAG PRICE MEDIAN
  DATES AIRLINE STOPS DURATION RETURN_PRICE CHANNEL FLIGHT_LINK HOTEL_LINK
  ESIM_LINK INSURANCE_LINK`.
- `[[ ... ]]` marks an optional part. It's removed if any value inside it is missing.
- A line with a missing value (outside `[[ ]]`) is removed, e.g. the "Kthimi"
  line when there's no return flight.
- Posts are sent as Telegram HTML, so links are written as `<a href="{FLIGHT_LINK}">Rezervo tani</a>`.

For an English version later: add `templates/en.txt` and set `language: en`.
The English month names etc. are already in `src/formatter.py`.

### Affiliate links (hotel, eSIM, insurance)

All link building is in `src/links.py`. The programs themselves are configured
under `links.partners` in config.yaml. Each one has:

- `url`: a template like `https://www.booking.com/searchresults.html?ss={city_en}&checkin={checkin}&checkout={checkout}`,
  or a ready-made link from the Travelpayouts link generator.
- `wrapper` *(optional)*: a tracking redirect like
  `https://tp.media/r?marker={marker}&trs=PROJECT_ID&p=PROGRAM_ID&campaign_id=CAMPAIGN_ID&u={url}`.

While `url` is empty, that link is simply left out of posts. Each partner
becomes a `{NAME_LINK}` placeholder, so you can add e.g. `car_rental` and use
`{CAR_RENTAL_LINK}` in the template.

> **Hotellook closed in October 2025**, so hotels need another Travelpayouts
> program (Booking.com, Trip.com, Agoda, …). Join one, then fill in `links.partners.hotel`.

## Data and repo size

`data/prices.db` is committed after every run (8× a day). To keep it small:
only the cheapest price per route, direction, and date is stored per run, and
rows older than `history_retention_days` (45) are deleted. If the repo ever
gets heavy, the next step is to move the DB to a separate `data` branch that
is force-pushed (no history kept).

## Premium channel (built, off by default)

A paid, private channel that gets deals **instantly**, with looser rules (30%
below the median instead of 40%) so it gets more of them. The free channel only
posts a deal once premium has had it for `free_delay_hours` (6), and adds a line
like "⚡ Anëtarët Premium e morën këtë ofertë 6 orë më parë · Bashkohu".

To turn it on:
1. Create a **private** Telegram channel and add the bot as an admin with **Post messages**.
2. Get its numeric id (it starts with `-100`): forward any post from the channel to
   [@userinfobot](https://t.me/userinfobot), or open the channel in Telegram Web,
   where the URL shows `#-100...`.
3. Add it as `TELEGRAM_PREMIUM_CHANNEL_ID` in `.env` and in the GitHub secrets.
4. Channel settings → Invite links → create a **paid subscription** link (Telegram
   Stars, monthly). Paste it as `premium.join_link` in config.yaml.
5. Set `premium.enabled: true`, then commit and push.

Posts for each channel are logged separately (`[premium]` / `[free]`) and
deduped separately. Premium's wording is in `templates/sq_premium.txt`.

## Exit codes

`0` OK · `1` the run failed (API down, bad token, a post failed) · `2` configuration
problem (missing env var, bad config.yaml). GitHub Actions marks the run red on 1 and 2.

## Roadmap (not built yet: where it plugs in)

- **Phase 2: Instagram/Facebook + deal images.** Add a publisher next to
  `telegram.py` and call it from `main.publish()`. `Deal` already carries
  everything an image generator needs. Use a new `channel` name (e.g. `"instagram"`)
  so it dedupes separately in `posted_deals`.
- **Phase 3b: custom alerts** ("tell me when TIA → LON is under €30"). Needs a bot
  that stores each subscriber's routes and sends them direct messages.
- **Phase 4: English channel for flights *into* Tirana.** Return-direction
  prices (DEST → TIA) are already being scanned and stored. Add `templates/en.txt`
  and a config where origin and destination are swapped.

## Troubleshooting

- **`Travelpayouts rejected the API token (HTTP 401)`**: check `TRAVELPAYOUTS_TOKEN`.
- **`bot is not a member of the channel` / `chat not found`**: add the bot as a channel
  admin with "Post messages", and check `TELEGRAM_CHANNEL_ID` starts with `@`.
- **No deals for days**: run `python -m src.main --dry-run -v` to see medians and
  prices per route; lower `deal_discount_pct` or raise thresholds.
- **A route always shows "prices for 0 dates"**: the Data API has no cached prices
  for it. Try the city code (e.g. `MIL`, `LON`) instead of the airport code.
