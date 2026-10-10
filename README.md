# FlyFromTirana ✈️

Scans cheap flights from Tirana (TIA), spots deals, and posts them to the
Telegram channel [@flyfromtirana](https://t.me/flyfromtirana) with affiliate
links. It runs every 3 hours in a Docker container on a small server. The same
run also renders a one-page **website** with the cheapest price to every
destination, which a second container serves. The paid premium channel gets its
own quick scan every 15 minutes in between.

## How it works

```
src/scheduler.py (every 3h, in the `scanner` container)
  └─ python -m src.main
       1. scanner.py   fetch prices TIA → each destination (and back) for the next 60 days
       2. storage.py   save them in data/prices.db (SQLite)
       3. deals.py     deal = ≥40% below the route's 30-day median, or under the route's € threshold
                       skip anything already posted in the last 7 days at the same price band
       4. formatter.py build the Albanian post from templates/sq.txt (links from links.py)
       5. telegram.py  send it to the channel with the destination's photo (best deals first)
  └─ python -m src.website
       6. website.py   rebuild docs/index.html from the database (every route, deals first)
src/scheduler.py (every 15 min in between, when premium is on)
  └─ python -m src.main --premium-only
       steps 1 and 3-5 for the premium channel only: nothing is saved, the free
       channel and the website are left alone
  data/prices.db and docs/ live in Docker volumes, so history and the site survive redeploys
```

Prices come from the Travelpayouts **Data API** (`aviasales/v3/prices_for_dates`).
These are *cached* prices that Aviasales users found recently, not live
availability. That's fine for spotting deals: the booking link opens a live search.

## Project layout

```
config.yaml              routes, deal rules, link templates, website settings (no secrets)
templates/sq.txt         post wording (Albanian)
templates/site.html      the website page (wording, CSS, a little JS); site_row.html = one route in the
                         list, site_card.html = one photo card, site_hero.html = one hero banner
assets/img/              the website's photos (dest/, services/) and credits.json (authors + licences)
assets/telegram/         the destination photos again, as JPEGs, sent with the Telegram posts
docs/                    the generated website (index.html + img/), what the `web` container serves
src/
  main.py                orchestrator + CLI (--dry-run, --routes)
  config.py              loads config.yaml + env vars
  scanner.py             Travelpayouts API client
  storage.py             SQLite: price history + posted deals
  deals.py               deal detection, dedupe, ranking
  links.py               ALL affiliate link building
  formatter.py           Deal → post text
  telegram.py            Telegram Bot API client
  photos.py              which photo goes on a route's posts, and its credit
  website.py             renders docs/index.html from the database (no API calls)
  scheduler.py           the server's loop: a scan, then the website, every 3 hours,
                         and quick premium scans in between
  http_client.py         shared retry/backoff for HTTP calls
scripts/post_test.py     send one test message to the channel
Dockerfile               the scanner image
docker-compose.yml       scanner + web containers and their two volumes
tests/                   pytest suite (HTTP is mocked; no network needed)
.github/workflows/       tests.yml (on push)
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

### 4. Run it on a server

`docker-compose.yml` runs two containers: `scanner` (the scheduler: a scan at
minute 17 of every third hour UTC, then the website build, plus a quick premium
scan every `premium.scan_every_minutes` in between) and `web` (serves the page).
With plain Docker:

```bash
cp .env.example .env          # the same values as for local runs
docker compose up -d --build
docker compose logs -f scanner
```

On Coolify: new resource → this repo → Docker Compose build pack, enter the same
variables under Environment Variables, and give the `web` service a domain.

- The price history lives in the `data` volume. A brand-new volume starts from
  the `data/prices.db` committed here, so the first run knows what was already
  posted and doesn't repeat the last week's deals.
- Try it without posting anything:
  `docker compose exec scanner python -m src.main --dry-run --routes BGY,VIE`
- Never run it in two places at once. Each copy keeps its own "already posted"
  list, so the channel would get every deal twice.

## Website

One static page, in Albanian, built from `data/prices.db` after every scan.
Its layout follows an airline booking page, top to bottom:

- **Sticky top bar** with the brand and a "Bashkohu në Telegram" button.
- **Hero carousel**: a brand slide (headline, today's lowest price, the best deal
  as a chip, live counts), then the three best deals as big banners. It moves on
  every 6.5 s and stops as soon as the visitor touches it.
- **Search card** over the hero: pick a destination, date, passengers and one-way
  or return. It highlights that route on the page, fills in its cheapest date, and
  "Kërko fluturime" opens the matching Aviasales search with your marker.
- **Zbulo destinacionet**: a carousel of photo cards, one per route, with the
  cheapest price, city, country and date. The whole card is the "Rezervo" link.
  Deals come first (biggest saving first) with a yellow price and saving sticker;
  they use the free channel's deal rules without the "already posted" filter.
- **Gjithçka për udhëtimin**: white photo cards for the partner links (eSIM,
  insurance, car rental, compensation, and hotels once `links.partners.hotel` has
  a url). A card whose link is empty is left out.
- **Të gjitha destinacionet**: every route, deals included, drawn as a boarding
  pass, with a sort menu (deals first, cheapest, biggest saving, soonest). The
  ticket shows the city, the TIA → airport route, the cheapest date (plus other
  dates within 10%), airline and the cheapest flight back; past the tear line, the
  stub holds the price, the usual price and "Rezervo" with your marker. Deals get a
  yellow stub and a red "OFERTË" stamp.
- A full-width "Mos humb asnjë ofertë" Telegram band with the Premium link (when
  enabled), and a footer with the affiliate disclosure and the photo credits.

Everything still works with JavaScript off: the carousels become plain scrolling
rows and the search button jumps to the list.

City and country names are in English ("Vienna, Austria"), on the website and in
the Telegram posts alike: both use each route's `city` from config.yaml.

Booking links carry the SubID `website` (posts use `telegram`), so Travelpayouts
stats show which one earned a click. Routes whose newest prices are older than
2 days are left off the page.

With the premium channel on, the page follows the **free channel**: a price that
is a deal for premium is left out until premium has had it for `free_delay_hours`,
which is when the free channel may post it too. Until then the route shows its
cheapest other date. So nobody can read a premium deal off the website early.

### Where it is served

The `web` container serves the `site` volume, which the scanner fills: once
when it starts, then after every scan. Point a domain at the `web` service in
the deploy tool (Coolify: its Domains field) and put that address in
`website.url` in config.yaml (it's linked at the bottom of every Telegram post and
used for the page's canonical/og:url tags).

### Preview locally

```bash
python -m src.website            # writes docs/index.html from your local prices.db
python -m src.website --no-premium-delay   # also show the deals premium is still getting early
open docs/index.html             # macOS; or open the file in any browser
python -m src.website --out /tmp/site   # somewhere else, leaving docs/ alone
```

### Photos

The photos live in `assets/img`: `dest/<iata>.webp` (600×840) for each route's card
and `services/<partner>.webp` (720×450) for the travel services. Every build copies
them to `docs/img`. They come from Openverse and Wikimedia Commons under licences
that allow commercial use (CC0, public domain, CC BY, CC BY-SA). Each one's author,
source and licence is in `assets/img/credits.json`, which the footer lists under
"Fotot dhe licencat e tyre", as CC BY and CC BY-SA require.

- **New route without a photo**: its card shows its country colours instead. To add
  one, save a 600×840 WebP as `assets/img/dest/<iata>.webp` (lowercase code) and add
  its entry to `credits.json`: `file`, `slot` (the IATA code), `title`, `creator`,
  `creator_url`, `source_url`, `license`, `license_url`.
- **Replacing a photo**: overwrite the file and update its entry in `credits.json`.

**Photos on the Telegram posts.** Each post is sent as the destination's photo with
the text under it. Telegram wants a JPEG, so every route's photo is also kept as
`assets/telegram/<iata>.jpg`. After adding or replacing a WebP, make its JPEG (macOS):

```bash
sips -s format jpeg -s formatOptions 82 assets/img/dest/vie.webp --out assets/telegram/vie.jpg
```

- A route without a JPEG gets a post without a photo. So does any post Telegram
  refuses the photo for (the log says why), or whose text is longer than the 1024
  characters allowed under a photo. A photo problem never loses a post.
- CC BY and CC BY-SA photos must name their author, so those posts end with a line
  like "📷 Foto: D-Stanley, CC BY 2.0", linked to the photo's source. It comes from
  `credits.json`; CC0 photos get no such line.
- `post_photos: false` in config.yaml sends text only, as before.

### Change the wording or look

- `templates/site.html` is the whole page (text, CSS and a little JavaScript).
  `templates/site_row.html` is one route in the list, `templates/site_card.html` one
  photo card in the destinations carousel, and `templates/site_hero.html` one banner
  in the hero carousel (the three best deals). All follow the post template rules
  (`{NAME}`, `[[optional]]`, a line with a missing value is dropped); HTML comments
  are stripped, so notes in the templates never reach the page. The row and hero
  templates can use every placeholder from `templates/sq.txt` plus `{IATA}`,
  `{DEAL}`, `{COUNTRY}` (lowercase code taken from the flag, e.g. `it`:
  it picks the card's colours), `{COUNTRY_NAME}` ("Austri"), `{PHOTO}` (the route's
  photo, if it has one), `{BEST_DATE}` (cheapest date, `YYYY-MM-DD`) and `{BEST_DAY}`
  (the same date as "14 Nën").
  The page template also gets `{MIN_PRICE}` (cheapest price on the page),
  `{DEAL_COUNT}`, `{ROUTE_COUNT}`, `{TOP_CITY}`/`{TOP_PRICE}`/`{TOP_SAVING}` (the best
  deal, shown as a chip on the first hero slide), `{DATE_MIN}`/`{DATE_MAX}` (the search
  card's date window), `{HERO_SLIDES}` and `{DESTINATION_OPTIONS}` (the `<option>` list
  for the search card's destination picker), `{DEST_CARDS}` (the photo cards) and
  `{PHOTO_CREDITS}` (the footer's credits list).
- The search card never shows availability: with JavaScript it builds a tracked
  Aviasales search link (route, date, passengers) from a "Rezervo" link's query
  string; without it the button simply jumps to the list.
- Deal detection on the site uses the same `deal_discount_pct` etc. as the posts.

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
  - { iata: PRG, city: Prague, flag: "🇨🇿", absolute_threshold_eur: 35 }
```

Use `airport:` for cities with several airports (it's shown as "MILAN (Bergamo)").
Try it with `python -m src.main --dry-run --routes PRG`.

### Change the post wording

Edit `templates/sq.txt`. Rules:

- `{NAME}` is replaced with a value: `CITY CITY_UPPER AIRPORT FLAG PRICE MEDIAN
  SAVING DATES AIRLINE STOPS DURATION RETURN_PRICE CHANNEL FLIGHT_LINK HOTEL_LINK
  ESIM_LINK INSURANCE_LINK COMPENSATION_LINK CAR_RENTAL_LINK PREMIUM_HOURS PREMIUM_LINK
  WEBSITE_LINK PHOTO_CREDIT PHOTO_LINK`. `SAVING` is the percent below the usual price
  ("68"); `WEBSITE_LINK` is `website.url` from config.yaml; `PHOTO_CREDIT` and
  `PHOTO_LINK` are the author, licence and source of the post's photo (see Photos).
- `[[ ... ]]` marks an optional part. It's removed if any value inside it is missing.
- A line with a missing value (outside `[[ ]]`) is removed, e.g. the "Kthimi"
  line when there's no return flight.
- Posts are sent as Telegram HTML, so links are written as `<a href="{FLIGHT_LINK}">Rezervo tani</a>`,
  and `<b>`, `<i>` and `<blockquote>` (the tinted box around the flight details) work too.
- Keep `<blockquote>` on a line that is always there (the dates) and `</blockquote>` on a
  line of its own. A tag on a line that gets removed would make Telegram refuse the post.
- An empty line in the template is an empty line in the post. If everything between two
  empty lines is left out, only one of them is kept.

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

## Data

`data/prices.db` grows with every full run (8× a day). To keep it small: only the
cheapest price per route, direction, and date is stored per run, and rows older
than `history_retention_days` (45) are deleted. The quick premium scans save no
prices at all, so they don't make it grow. On the server the file lives in
the `data` volume; the copy committed here is only the starting point for a new
volume, and the database local runs use.

## Premium channel (built, off by default)

A paid, private channel that gets deals **within minutes**, with looser rules (30%
below the median instead of 40%) so it gets more of them. The free channel only
posts a deal once premium has had it for `free_delay_hours` (6), and adds a line
like "⚡ Anëtarët Premium e morën këtë ofertë 6 orë më parë · Bashkohu". The
website waits the same way (see Website above).

Premium doesn't wait for the 3-hour runs: every `premium.scan_every_minutes` (15)
the scheduler runs `python -m src.main --premium-only`, which checks the prices
and posts any new deal to premium straight away (never during quiet hours). That
is up to about 150 API calls per scan, far below Travelpayouts' limit of 600 a minute.
`premium.max_posts_per_run` (4) now counts per scan, so it no longer caps a whole
day; lower it if the channel gets too busy. Set `scan_every_minutes: 0` to go back
to posting only with the 3-hour runs. The free channel still posts at its first
3-hour run after the 6 hours, so its wait is between about 5.5 and 8.5 hours
(longer when that run falls in quiet hours).

To turn it on:
1. Create a **private** Telegram channel and add the bot as an admin with **Post messages**.
2. Get its numeric id (it starts with `-100`): forward any post from the channel to
   [@userinfobot](https://t.me/userinfobot), or open the channel in Telegram Web,
   where the URL shows `#-100...`.
3. Add it as `TELEGRAM_PREMIUM_CHANNEL_ID` in `.env` and in the server's environment variables.
4. Channel settings → Invite links → create a **paid subscription** link (Telegram
   Stars, monthly). Paste it as `premium.join_link` in config.yaml.
5. Set `premium.enabled: true`, then commit, push and redeploy.

Posts for each channel are logged separately (`[premium]` / `[free]`) and
deduped separately. Premium's wording is in `templates/sq_premium.txt`.

## Exit codes

`0` OK · `1` the run failed (API down, bad token, a post failed) · `2` configuration
problem (missing env var, bad config.yaml). The scheduler logs any non-zero code as an error.

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
