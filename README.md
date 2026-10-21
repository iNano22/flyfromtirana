# FlyFromTirana ✈️

Scans cheap flights from Tirana (TIA), spots deals, and posts them to the
Telegram channel [@flyfromtirana](https://t.me/flyfromtirana) with affiliate
links. It runs every 3 hours in a Docker container on a small server. The same
run also renders the **website**: a main page with the cheapest price to every
destination, plus a page per city with its prices and a guide, in Albanian,
English and Italian. A second container serves it. The paid premium channel gets its own quick scan every 15
minutes in between.

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
          social.py    at 12:00 and 19:30 (social.post_times): the best deal on the site that
          + meta.py    Instagram/Facebook haven't had, as a card + story + Facebook post
  └─ python -m src.website
       6. website.py   rebuild docs/index.html from the database (every route, deals first)
                       and one page per city (docs/milan/, docs/rome/, ...) from content/destinations/
src/scheduler.py (every 15 min in between, when premium is on)
  └─ python -m src.main --premium-only
       steps 1 and 3-5 for the premium channel only, plus the Instagram/Facebook post
       whose time has come: nothing is saved, the free channel and the website are left alone
  data/prices.db and docs/ live in Docker volumes, so history and the site survive redeploys
```

Prices come from the Travelpayouts **Data API** (`aviasales/v3/prices_for_dates`).
These are *cached* prices that Aviasales users found recently, not live
availability. That's fine for spotting deals: the booking link opens a live search.

## Project layout

```
config.yaml              routes, deal rules, link templates, website settings (no secrets)
templates/sq.txt         post wording (Albanian); sq_premium.txt = the premium channel's,
                         sq_social.txt = the Instagram caption, sq_facebook.txt = the Facebook post,
                         sq_alert.txt = a price alert
templates/site.html      the website page (layout, CSS, a little JS); site_row.html = one route in the
                         price table, site_card.html = one photo card, site_hero.html = one line of
                         the board at the top, site_premium.html = the block about the Premium channel
templates/strings/       the website's wording, one file per language (sq.yaml, en.yaml, it.yaml)
templates/site_dest.html a city's own page; site_dest_fare.html = one airport's price on it;
                         site_guide.html = a city's photo tile on the main page;
                         site_analytics.html = Google Analytics and its cookie box;
                         site_drive.html = the Travelpayouts Drive script;
                         site_lang.html = the language dropdown
templates/site_404.html  the "page not found" page
templates/site_privacy.html  the privacy page (/privacy/): what the site collects and who gets it
content/destinations/    the city guides: one folder per language, one file per city in each
content/countries/       the entry documents for each country, one file per language
content/intro_posts.yaml the nine introduction posts for Instagram and Facebook (texts and card contents)
assets/social/intro/     the website screenshot shown on the introduction posts' phone
assets/img/              the website's photos (dest/, services/) and credits.json (authors + licences);
                         share/ = the picture a link to the site shows on Telegram or WhatsApp
assets/telegram/         the destination photos again, as JPEGs, sent with the Telegram posts
assets/fonts/            Archivo and IBM Plex Mono (SIL Open Font License), for drawing the deal cards
assets/music/            the Reels' music: 17 Pixabay tracks (50 s each) and credits.json
docs/                    the generated website (index.html + img/), what the `web` container serves
src/
  main.py                orchestrator + CLI (--dry-run, --routes): scan, post, then the social drafts
  config.py              loads config.yaml + env vars
  scanner.py             Travelpayouts API client
  storage.py             SQLite: price history + posted deals
  deals.py               deal detection, dedupe, ranking
  links.py               ALL affiliate link building
  formatter.py           Deal → post text
  telegram.py            Telegram Bot API client
  photos.py              which photo goes on a route's posts, and its credit
  cards.py               draws the Instagram/Facebook pictures: deal cards, the weekly cover, guide cards
  intro_cards.py         the nine one-off introduction cards (photo-led, big numbers, phone mock-ups)
  social.py              a social media post (deal, weekly top, city guide): pictures, captions, posting or drafting
  growth.py              the free channel's join links, the WhatsApp share link, and the weekly report
                         on both channels (who joined through which link, the member counts)
  reels.py               turns a deal card into a 10-second Instagram Reel (Pillow frames + ffmpeg, music)
  bot.py                 the bot's private chat: people set price alerts; it also notes channel joins
  alerts.py              sends the price alerts after each scan
  trips.py               your own trip watches (trip_watches): a cheap trip there and back, only to your chat
  meta.py                Meta Graph API client: posts to Instagram and the Facebook Page
  website.py             renders docs/index.html and the city pages from the database (no API calls)
  guides.py              loads the city guides from content/destinations/
  i18n.py                the website's languages: loads a language's wording into the templates
  scheduler.py           the server's loop: a scan, then the website, then the channel stats,
                         every 3 hours, and quick premium scans in between
  http_client.py         shared retry/backoff for HTTP calls
scripts/post_test.py     send one test message to the channel
scripts/share_card.py    make the pictures in assets/img/share (from share_card.html, with Chrome)
scripts/alert_chat.py    find your chat for the failure alerts, and send a test alert there
scripts/social_preview.py  draw sample Instagram/Facebook cards into ig_preview/, to check the look
scripts/social_test.py   scan today's prices and make one social media post now (draft or --publish)
scripts/meta_check.py    check META_PAGE_TOKEN: which Page and Instagram account it opens
scripts/intro_posts.py   the nine posts that open the accounts (content/intro_posts.yaml): draw, draft, post
scripts/invite_links.py  make the free channel's invite links, one per place that advertises it
Dockerfile               the scanner image
web/                     the web server image: Caddy and its settings (Caddyfile)
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
scan every `premium.scan_every_minutes` in between) and `web` (serves the page:
Caddy, set up in `web/Caddyfile`). With plain Docker:

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

#### Alerts

When a scan or the website build fails, the bot tells you on Telegram: once when it
starts failing (with the exit code and what it means) and once when it works again,
so a long outage is two messages, not one every 15 minutes. The quick premium scans
count as the scan. To switch it on:

1. Open the bot (the one that posts to the channel) in Telegram and press Start.
2. Run `python scripts/alert_chat.py` locally (it needs `TELEGRAM_BOT_TOKEN` in
   `.env`). It shows your chat's number.
3. Set `TELEGRAM_ALERT_CHAT_ID` to that number: in `.env` and in Coolify's
   Environment Variables, then redeploy. `python scripts/alert_chat.py --test`
   sends a test alert. A private channel where the bot is an admin works too.

Without `TELEGRAM_ALERT_CHAT_ID` failures are only in the scanner's logs. The
alerts don't cover the whole server going down; for that, Coolify can notify you
itself (its Notifications settings have Telegram).

## Website

Static pages built from `data/prices.db` after every scan, in three languages:
Albanian at the top of the site (`/`, `/milan/`), English under `/en/` and
Italian under `/it/` (see [Languages](#languages) below). The main page's
look is airport signage: signal yellow, black and white on a light grey floor,
with airport codes in a monospace face. Yellow always means a deal or the way
to one. Top to bottom:

- **Black top bar** (sticky) with the brand, the section links (wide screens), the
  language dropdown and a yellow "Bashkohu në Telegram" button. The dropdown shows
  the page's language as a code (AL, EN, IT) and opens a list that leads to the
  same page in the other languages. The footer has the same links as a row.
- **Yellow hero**: the headline, live counts and today's lowest price on the left;
  on the right a black board, "Çmimet më të mira sot", with one line for each of
  the page's first five routes (deals first), like a departures board. Each line
  is a "Rezervo" link. A phone shows the first three lines.
- **Search card** over the end of the hero: pick a destination, date, passengers
  and one-way or return. It marks that route on the page, fills in its cheapest
  date, and "Kërko fluturime" opens the matching Aviasales search with your marker.
- **Zbulo destinacionet**: photo cards, one per route, with the route's code on the
  photo and the cheapest price, city, country and date underneath. The whole card
  is the "Rezervo" link. Deals come first (biggest saving first) with the saving
  on yellow; they use the free channel's deal rules without the "already posted"
  filter. A phone swipes through all of them; a wide screen shows the first two
  rows, and the table below has every route.
- **Premium block** (only while `premium.enabled` and a `join_link` are set): "Merri
  ofertat në kohë reale", with the button to join and who gets a deal first. Its
  numbers come from config.yaml: `premium.free_delay_hours`,
  `premium.scan_every_minutes` and `quiet_hours`.
- **Të gjitha destinacionet**: every route, deals included, as a price table with
  tabs (all / deals) and a sort menu (deals first, cheapest, biggest saving,
  soonest). A line has the airport code, the city, the cheapest date (plus other
  dates within 10%), the cheapest flight back, the airline, the saving, the price
  with the usual price under it, and "Rezervo" with your marker. Deals get an
  "OFERTË" tag and their price on yellow. On a phone a line becomes a compact block.
- **Si funksionon**: the three steps.
- **Udhëzues për qytetet**: one photo tile per city page.
- **Gjithçka për udhëtimin**: white photo cards for the partner links (eSIM,
  attraction tickets, airport transfer, car rental, luggage storage, compensation,
  and tours, insurance and hotels once their `links.partners` entry has a url). A card
  whose link is empty is left out.
- A full-width yellow "Mos humb asnjë ofertë" Telegram band with the Premium line
  (when enabled), and a black footer with the affiliate disclosure.

Everything still works with JavaScript off: the tabs and the search button simply
jump to the list.

City and country names are in English ("Vienna, Austria"), on the website and in
the Telegram posts alike: both use each route's `city` from config.yaml.

The site's partner links (eSIM, insurance, …) are its own, made in its own
Travelpayouts project, and so are its flight links: see `website.links` in
config.yaml. Travelpayouts shows the two projects' clicks and earnings separately
(the posts' links also carry the SubID `telegram`). Routes whose newest prices are older than
2 days are left off the page.

With the premium channel on, the page follows the **free channel**: a price that
is a deal for premium is left out until premium has had it for `free_delay_hours`,
which is when the free channel may post it too. Until then the route shows its
cheapest other date. So nobody can read a premium deal off the website early.

### Languages

`website.languages` in config.yaml lists the site's languages (`[sq, en, it]`).
The first one is the main one, with its pages at the top of the site; every other
language gets a folder: `/en/`, `/en/milan/`, `/it/`, `/it/milan/`. Every page
links to itself in the other languages and tells search engines about them
(`hreflang`), and the sitemap lists all of them. The Telegram posts are not part
of this: they stay in `language` (Albanian).

A language is made of three things:

- **The wording of the pages**: `templates/strings/<code>.yaml`. The templates hold
  no text themselves: `{T_NAME}` in a template stands for the entry `NAME` of the
  language's file. To change a sentence, edit it in that file (in all three, if the
  meaning changes). All files must have the same entries; a test checks it.
- **The city guides**: `content/destinations/<code>/<slug>.yaml`, one per city, plus
  `content/countries/<code>.yaml` for the entry documents. A translated guide has
  the same parts and the same number of entries as the Albanian one, and keeps every
  number (prices, minutes); tests check that too. It may give the city's name in its
  language (`name: Milano`), which the Italian pages then use everywhere; `city`
  and `slug` stay the same in every language, so the address is `/it/milan/`.
- **Month names and a few flight words** ("direkt", "me ndalesë"): `WORDS` in
  `src/formatter.py`.

So when you **change a guide**, change it in all three folders. When you **add a
city**, add its guide in all three; until a language has it, that language simply
has no page for the city, and the language dropdown on the city's other pages leads
to that language's main page instead.

**Add a language**: copy `templates/strings/en.yaml` to `<code>.yaml` and translate
it, translate the guides into `content/destinations/<code>/` and
`content/countries/<code>.yaml`, add the language to `WORDS` (src/formatter.py) and
to `LANGUAGE_NAMES` and `LANGUAGE_CODES` (src/i18n.py: its name and the short code
the dropdown shows), then add the code to `website.languages`.

The dropdown itself is `templates/site_lang.html`. Albanian is shown there as "AL",
the code people know, while its folder and its code for browsers stay `sq`.

### City pages

Every city has a page of its own (`/milan/`, `/rome/`, ... on the site): the
current cheapest price for each of its airports on top, then a full guide of
roughly 1,000 words: what to see, a three-day plan, where to stay, what to eat,
how to get from each airport to the centre, getting around, day trips, typical
prices, when to go, tips and a few questions and answers. A row of jump links
under the prices leads to each part, and "Destinacione të tjera" at the end shows
the other cities as the same photo tiles the main page uses. The main page links to them from the "Udhëzues për qytetet"
section (a wide photo tile per city, with a short teaser and the cheapest price:
a row to swipe on phones, a grid on larger screens) and from every ticket in the
list. These pages are what search engines
can find the site by, so the build also writes `sitemap.xml` and `robots.txt`
(they need `website.url`).

The text of a guide is one file per language, `content/destinations/<language>/<slug>.yaml`
(`sq/milan.yaml` explains the shape):

- **Change a guide**: edit its file. The page is rebuilt with the next run. Only
  `city`, `slug` and `intro` are required; any other part (`sights`, `itinerary`,
  `areas`, `food`, `airports`, `transport`, `daytrips`, `budget`, `when`, `tips`,
  `faq`) can be left out, and then it is not on the page.
- **Entry documents**: the answer to "Çfarë dokumentesh duhen?" is the same for
  every city of a country, so it lives in `content/countries/<language>.yaml`, one
  entry per country. Review it when the rules change (the EU's ETIAS above all).
- **New route in a city that has a guide**: add the airport under `airports:`
  in that city's file (a test fails until every airport of a city is covered).
- **New city**: add a file. `city` must be exactly the route's `city` in
  config.yaml, and `slug` (lowercase letters, digits, hyphens) becomes the address.
  Until a city has a file it simply has no page.
- **Its photo**: a wide photo of the city, `assets/img/guides/<slug>.webp`
  (960×600), is used on its tile and at the top of its page. It is a different
  picture from the route's tall card photo. Without one, the tile has a
  plain black top and the page uses the route's photo.
- `tagline` in the file is the short line under the city's name on its tile.
- The prices in the guides (transport, tickets, food, a day's budget) are rough
  figures from 2026. They go out of date: review them once or twice a year.

### Google Analytics

To count visitors, create a Google Analytics 4 property for the site at
[analytics.google.com](https://analytics.google.com), add a **Web** data stream,
and paste its Measurement ID (`G-XXXXXXXXXX`) into `website.google_analytics_id`
in config.yaml. Then commit, push and redeploy.

Analytics sets cookies, so every page shows a small box about it. It works in one
of two ways, chosen by `website.google_analytics_ask_first` in config.yaml:

- `false` (what config.yaml has now): every visitor is counted from the first
  page view. The box tells them so, and "Refuzoj" stops Analytics, removes its
  cookies and keeps it off for that visitor.
- `true`: nothing is loaded from Google until the visitor presses "Pranoj". This
  is what the EU's cookie rules (and Google's own terms for visitors from the EU)
  ask for, but everyone who ignores the box goes uncounted, so the numbers are
  much lower than the real traffic.

Either way the answer is remembered in the visitor's browser, and "Cilësimet e
cookies" in the footer lets them change it. With the ID empty there is no
Analytics, no box and no cookie.

### Privacy page

`/privacy/` (and `/en/privacy/`, `/it/privacy/`) says what the site collects and who
gets it: Cloudflare, Google Analytics, Google Fonts, GetYourGuide's script, the
partner links and Telegram, plus the visitor's rights. It is linked from every
footer and from the cookie box. Its text is the `PRIVACY_` entries in
`templates/strings/<language>.yaml`; a part about something the site doesn't use
is left out by itself (Analytics without an ID, GetYourGuide without a Partner ID).
`website.operator` and `website.contact_email` in config.yaml say who runs the site
and where to write about personal data.

When the site starts using something new that collects data (a new script, a
widget, a form), add it to the text in all three languages and change the date in
`PRIVACY_UPDATED`.

### GetYourGuide's widget

Every city page can show GetYourGuide's activity widget under its guide: a grid
of that city's tours and activities with photos, which GetYourGuide fills in and
keeps up to date itself (`id="ture"` in `templates/site_dest.html`).

- `website.getyourguide_activities` in config.yaml is how many activities it
  shows; `0` takes the widget off.
- It has no settings of its own beyond that: the Partner ID and the city are read
  from the city's tours link (`links.partners.tours` and `links.city_pages.tours`,
  see Affiliate links). The number after `-l` in a city's address there
  (`rome-l33`) is the city's number at GetYourGuide. So the widget only appears
  once the tours link is switched on, and always earns under the same ID.
- It is shown in Italian on the Italian pages and in English on the others
  (`GYG_LOCALE` in `templates/strings/<language>.yaml`): GetYourGuide has no Albanian.
- The widget draws itself in a frame loaded from getyourguide.com.
- Every page that has the tours link (the main page and the city pages) loads
  GetYourGuide's script from its `<head>`, with the Partner ID. It is the one their
  dashboard calls "GetYourGuide Analytics": it counts the pages' visits for their
  statistics, and on the city pages it also draws the widget.

### Travelpayouts Drive

Drive is Travelpayouts' own script for a site: it turns travel words in the text
into affiliate links and can add offers and link previews. When its address is
set, every page of the site loads it from its `<head>` (`templates/site_drive.html`).

> **Switched off since October 2026.** Travelpayouts could not connect the project
> to Booking.com and Viator, and Drive's keyword links do not work without those
> programs. The script's address is kept in a comment in config.yaml, to put it
> back once they are open to the project.

- The script belongs to the "Flyfromtirana" project. Its address is
  `website.travelpayouts_drive_script` in config.yaml: the one inside
  `script.src = '...'` in the snippet the dashboard shows under Drive.
- What Drive does on the pages (keyword links, inserted recommendations, offers
  in a background tab, previews) is switched on and off in the Travelpayouts
  dashboard, not in this repo. Its results are under Drive → Content analytics.
- Set the address to `""` to take Drive off the site.

### Where it is served

The `web` container serves the `site` volume, which the scanner fills: once
when it starts, then after every scan. Point a domain at the `web` service in
the deploy tool (Coolify: its Domains field) and put that address in
`website.url` in config.yaml (it's linked at the bottom of every Telegram post and
used for the page's canonical/og:url tags).

An address that doesn't exist gets the site's own "page not found" page
(`templates/site_404.html`), in the language of the folder it was asked in:
`/en/anything` gets `/en/404.html`, everything else `/404.html`. The build writes
those files and `web/Caddyfile` tells the server to use them. A mistake in the
Caddyfile stops the image build, so the running site stays up.

### Preview locally

```bash
python -m src.website            # writes docs/index.html from your local prices.db
python -m src.website --no-premium-delay   # also show the deals premium is still getting early
open docs/index.html             # macOS; or open the file in any browser
python -m src.website --out /tmp/site   # somewhere else, leaving docs/ alone
python -m http.server --directory docs  # then http://localhost:8000 : needed to click through to the city pages
```

### Photos

The photos live in `assets/img`: `dest/<iata>.webp` (600×840) for each route's card,
`guides/<slug>.webp` (960×600) for each city's guide, and `services/<partner>.webp`
(720×450) for the travel services. Every build copies
them to `docs/img`. Most destination photos come from [Unsplash](https://unsplash.com)
(downloaded from the website, under the Unsplash License, which allows commercial
use and asks for no credit); the rest are CC0 or CC BY from Openverse and Wikimedia
Commons. Each one's author, source and licence is kept in `assets/img/credits.json`
as a record of where it came from.

The page shows no photo credits, because none of the photos on it requires one.
Only a CC BY or CC BY-SA photo does: while one is on the page, the footer lists it
under "Fotot dhe licencat e tyre". Today that is just the hotel card's photo, and
that card only appears once a hotel link is set.

**The share picture.** When someone pastes a link to the main page into Telegram,
WhatsApp or Facebook, the preview shows `assets/img/share/<language>.png` (1200×630):
the headline in that language on the yellow sign, next to a small departures board.
It is not a photo but a drawing of `scripts/share_card.html`, so after changing the
headline (`KICKER`, `HEADLINE_A`, `HEADLINE_B` in `templates/strings/`) make the
pictures again:

```bash
python scripts/share_card.py     # needs Google Chrome and an internet connection (for the fonts)
```

City pages show their own photo instead. Apps keep a link's preview for a while, so
an old link may show the old picture for some days.

**New photos: take free ones from Unsplash**, from the website (not Unsplash+, and
not through the Unsplash API: the API's own rules require crediting the photographer
wherever a photo is shown). Then nothing needs a credit, on the page or in a post.

- **New route without a photo**: its card has a plain black top instead. To add
  one, save a 600×840 WebP as `assets/img/dest/<iata>.webp` (lowercase code) and add
  its entry to `credits.json`: `file`, `slot` (the IATA code), `title`, `creator`,
  `creator_url`, `source_url`, `license`, `license_url`.
- **Replacing a photo**: overwrite the file and update its entry in `credits.json`.

**Photos on the Telegram posts.** Each post is sent as the destination's photo with
the text under it. Telegram wants a JPEG, so every route's photo is also kept as
`assets/telegram/<iata>.jpg` (1080×1512 is a good size; the website's WebP is the
same picture at 600×840). To make one from the other (macOS; `cwebp` is from
`brew install webp`):

```bash
cwebp -q 72 -resize 600 840 assets/telegram/vie.jpg -o assets/img/dest/vie.webp   # JPEG -> the site's WebP
sips -s format jpeg -s formatOptions 82 assets/img/dest/vie.webp --out assets/telegram/vie.jpg   # or WebP -> JPEG
```

- A route without a JPEG gets a post without a photo. So does any post Telegram
  refuses the photo for (the log says why), or whose text is longer than the 1024
  characters allowed under a photo. A photo problem never loses a post.
- No destination photo needs a credit today (they are Unsplash or CC0), so posts
  carry none. If you add a CC BY or CC BY-SA photo, its posts end with a line like
  "📷 Foto: Jane Doe, CC BY 2.0", linked to the photo's source, because those
  licences require naming the author. It is read from `credits.json`.
- `post_photos: false` in config.yaml sends text only, as before.

### Change the wording or look

- **Wording**: every sentence on the pages is in `templates/strings/<language>.yaml`
  (see [Languages](#languages)). In the templates `{T_NAME}` marks where a text goes.
- **Look**: `templates/site.html` is the whole page (layout, CSS and a little JavaScript).
  `templates/site_row.html` is one route in the price table, `templates/site_card.html`
  one photo card, `templates/site_hero.html` one line of the board at the top (the
  page's first five routes; `HERO_ROWS` in `src/website.py`) and
  `templates/site_premium.html` the block about the Premium channel. All follow the
  post template rules (`{NAME}`, `[[optional]]`, a line with a missing value is
  dropped); HTML comments are stripped, so notes in the templates never reach the
  page. The row, card and board templates can use every placeholder from
  `templates/sq.txt` plus `{IATA}`, `{DEAL}`, `{COUNTRY_NAME}` ("Austri"), `{PHOTO}`
  (the route's photo, if it has one), `{BEST_DATE}` (cheapest date, `YYYY-MM-DD`)
  and `{BEST_DAY}` (the same date as "14 Nën").
  The page template also gets `{MIN_PRICE}` (cheapest price on the page),
  `{DEAL_COUNT}`, `{ROUTE_COUNT}`, `{DATE_MIN}`/`{DATE_MAX}` (the search card's date
  window), `{HERO_ROWS}` and `{DESTINATION_OPTIONS}` (the `<option>` list for the
  search card's destination picker), `{DEST_CARDS}` (the photo cards), `{PREMIUM}`
  (the Premium block) and `{PHOTO_CREDITS}` (the footer's credits, only there when
  a photo requires one).
  The colours are named once, at the top of each page's CSS (`--sun` is the yellow,
  `--ink` the black): change them there, in `site.html`, `site_dest.html` and
  `site_404.html` (the "page not found" page).
- **The top bar on narrow screens**: when the menu links and the yellow Telegram button
  don't fit side by side (a tablet, a small laptop window), the button shows the paper
  plane alone. The widths are in each page's CSS, with the reason next to them. A longer
  menu word or a new link needs them checked again, in all three languages.
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
  ESIM_LINK INSURANCE_LINK COMPENSATION_LINK CAR_RENTAL_LINK TICKETS_LINK TRANSFER_LINK
  LUGGAGE_LINK TOURS_LINK PREMIUM_HOURS PREMIUM_LINK
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

**A partner's own page for a city.** Tiqets (`tickets`), Welcome Pickups
(`transfer`) and Radical Storage (`luggage`) have a page per city, listed under
`links.city_pages` in config.yaml. A post about a flight to Milan, and the site's
Milan page, link to the partner's Milan page; the site's main page, which is about
no one city, links to the partner's `url`. For this to be counted, the partner's
`wrapper` is its Travelpayouts short link with `?u={url}` added, which opens the
given page instead of the homepage. A new route needs its city added to each
partner in that list (a test checks that none is missing); until then its links
open the partner's homepage.

**GetYourGuide (`tours`) is not a Travelpayouts program.** It is GetYourGuide's own
partner program (partner.getyourguide.com), and it has no short link: a link is
the page's own address with your Partner ID added to its end. Its `wrapper` is
therefore `{plain_url}?partner_id=<your ID>&cmp={sub_id}`, where `{plain_url}` is
the address as it is (not encoded, as `{url}` is for a redirect) and `cmp` is the
campaign name GetYourGuide's statistics show: `telegram` for the posts, `website`
for the site. It has a page per city too, under `links.city_pages`. The posts get
a "Ture dhe aktivitete në <city>" line; the site gets a card, the links on the
city pages and the widget (see GetYourGuide's widget).

**The website has its own links.** Travelpayouts counts clicks per project, and the
site is its own project ("Flyfromtirana") next to the Telegram one. Links made in
the site's project go under `website.links` in config.yaml, which has the same
shape as `links`; whatever isn't set there falls back to the link the posts use.
A new program on the site therefore needs a link generated in the site's project.
The site's flight links use its Aviasales short link as a redirect
(`website.links.flight.wrapper`): `https://aviasales.tp.st/...?u=<search page>`
opens that search instead of the homepage and counts the click under the site's project.

> **Hotellook closed in October 2025**, so hotels need another Travelpayouts
> program (Booking.com, Trip.com, Agoda, …). Join one, then fill in `links.partners.hotel`.
>
> **EKTA closed in October 2026**, so travel insurance is switched off the same way
> (`links.partners.insurance` is empty). The two insurance programs left in
> Travelpayouts, VisitorsCoverage and Insubuy, were not open to these projects yet.

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

The wait is per price, not per date. When a date premium was already sent falls
into a cheaper price band (`price_band_eur`), premium gets the new price first, and
the free channel's 6 hours and the hours in its post count from that post. The
number in a free post is often much larger than 6: the free channel posts 2 deals a
run, best saving first, so a weaker deal can wait a day or two for its turn.

Premium doesn't wait for the 3-hour runs: every `premium.scan_every_minutes` (15)
the scheduler runs `python -m src.main --premium-only`, which checks the prices
and posts any new deal to premium straight away (never during quiet hours), and
makes the Instagram/Facebook post whose time has come (see below). That
is up to about 150 API calls per scan, far below Travelpayouts' limit of 600 a minute.
`premium.max_posts_per_run` (1) counts per scan, so premium gets the best new deal
of each scan: up to 4 posts an hour. Set `scan_every_minutes: 0` to go back
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

## Instagram and Facebook posts

Twice a day, at `social.post_times` (12:00 and 19:30 Tirana, when people are on
their phones), the best deal on the website that the accounts haven't had goes
out on Instagram and Facebook (`social` in config.yaml): a feed card (1080×1350,
the tallest picture Instagram's feed takes; Facebook takes it too) and a story
card (1080×1920), drawn by `src/cards.py` in the website's look, with a caption.
One post per time, never two in a row. The posts are made by the bot itself once
`META_PAGE_TOKEN` is set; until then, or whenever a post fails, you get them as a
**draft** on Telegram to post by hand.

**By the bot.** At a posting time (the first scan at or after it: the quick premium
scans count, so within 15 minutes), the bot:
1. puts the two pictures on the website under `/social/` (Instagram only fetches
   pictures from a public address; they are removed after `social.keep_pictures_days`),
2. publishes the feed card as an Instagram post with the caption from
   `templates/sq_social.txt` (no link: Instagram captions aren't clickable, so it
   says "linku në bio" — put `flyfromtirana.al` in the account's bio), then as a story,
3. publishes the feed card on the Facebook Page with `templates/sq_facebook.txt`,
   which holds the booking link (SubID `social.sub_id`, `facebook`, so its clicks show
   apart from the channel's and the website's in Travelpayouts),
4. sends a report to your chat (`TELEGRAM_DRAFTS_CHAT_ID`) with the posts' links.
   A post that failed (Meta refused it, the token stopped working, ...) is in the
   report with the reason, and the draft follows so you can make it by hand.

`social.publish` says where the bot posts (`instagram`, `facebook`, or fewer).

**Reels.** With `social.reels: true` a deal goes to Instagram as a **Reel** instead of
the feed picture: Instagram shows Reels to people who don't follow the account yet,
feed posts mostly to followers. `src/reels.py` brings the deal card to life in 10
seconds (1080×1920): the photo slowly zooms in, the route, the giant price, the city,
the saving sticker, the dates and the flight come in one after the other, and a yellow
closing card says "Oferta të reja çdo ditë · Rezervo: linku në bio · Njoftime falas në
Telegram". The words keep clear of what Instagram lays over a Reel (the `reel` layout
in `src/cards.py`). The Reel's cover, the frame with everything in place, is what the
profile grid shows. ffmpeg makes the MP4 (the Dockerfile installs it); without ffmpeg,
or if Instagram refuses the video, the deal goes out as the feed picture as before, and
the report says so. The Instagram story is the same video as the Reel (when the Reel
went out; if Instagram refuses the video as a story, the story picture goes instead).
The Facebook post keeps the picture.

The sound is one of the 17 tracks in `assets/music` (free tracks from Pixabay under the
Pixabay Content License, none registered with Content ID, each cut to 50 energetic
seconds; authors and pages in `credits.json`). They take turns, one per Reel, and each
time the list comes round the next 10 seconds of a track play, so the same sound comes
back only after 85 Reels. To add a track: put the file in `assets/music` and a line in
`credits.json`. Instagram's own music library can't be used through the API.
`python scripts/social_preview.py --reels` makes the sample deals' Reels in
`ig_preview/reels/`, to watch them before any goes out.

**Two more kinds of post keep the accounts busy** (same mechanics, same report):
- **The week's top deals**: on `social.weekly_day` (Sunday) the day's first post is a
  cover card listing the best `social.weekly_count` (5) deals the website shows, plus
  each deal's card, posted together as one Instagram carousel and one Facebook post
  with all the pictures; the evening's post is a deal as usual. It needs at least 3
  deals on the site (otherwise a deal is posted instead); the caption lists them with
  their dates. Remembered in `posted_deals` as `social_weekly`.
- **A city guide**, at the day's last posting time when the day had no post at
  all: a carousel of up to 8 cards, from the guide on
  the website — a cover with the city's photo, name, tagline, the cheapest current
  flight and the page's address (`flyfromtirana.al/rome/`), then a card per section
  (what to see, the 3-day plan, where to stay, what to eat, airports and transport,
  day trips, practical tips; entries that don't fit a card are left off) — plus the
  cover as a story. The Facebook post carries the same pictures and links to the page. The cities with a guide in the
  posts' language take turns, the one posted longest ago first. Remembered as
  `social_guide` (one row per city, under its airport code). An empty `weekly_day`
  switches the weekly post off.

**As a draft** (no `META_PAGE_TOKEN`): three messages in your chat: the two pictures
as *files*, not photos, so Telegram keeps them at full size (save them to the phone's
gallery and post them as they are); the caption to copy; and the booking link for
the Facebook post.

Which deals: what the website lists as deals (the free channel's rules, and like the
free channel it waits for prices premium is still alone with), biggest saving first,
skipping a city already posted that day and a city posted at the same or a cheaper
price band within `repost_cooldown_days`, whatever the date or the airport (they are
remembered in `posted_deals` under the name `social`). A cheaper price is news again.
A posting time stays open until the next one, so a post that missed its scan (a
redeploy, say) still goes out; never during quiet hours. A post counts as made once
it is posted or its draft reached you. Only a Telegram failure is left unrecorded
(the next scan tries again). None of this holds up the channel posts, which are
already out.

To turn it on:
1. Open the bot in Telegram and press Start, then run `python scripts/alert_chat.py`
   to see your chat's number (the same steps as for the failure alerts above).
2. Set `TELEGRAM_DRAFTS_CHAT_ID` to that number in `.env` and in Coolify's Environment
   Variables. Left empty, the messages go to `TELEGRAM_ALERT_CHAT_ID`; with neither set
   the bot logs a warning and sends nothing. A private channel where the bot is an admin
   works too.
3. `social.enabled: true` in config.yaml (it is). That gives you the drafts.
4. For the bot to post itself, make the Page token (once; it doesn't expire):
   - The Instagram account must be a **Business** account linked to a Facebook Page
     (Page settings → Linked accounts). Stories through the API need Business, not Creator.
   - On developers.facebook.com create an app (type Business) with the use cases "Manage
     everything on your Page" and "Manage messaging & content on Instagram"; under each
     use case's permissions add `pages_show_list`, `pages_read_engagement`,
     `pages_manage_posts`, `instagram_basic`, `instagram_content_publish`,
     `business_management`. Settings → Basic: Privacy Policy URL
     `https://flyfromtirana.al/privacy/`. The app stays in Development mode: for your
     own accounts Meta needs no App Review.
   - Graph API Explorer (developers.facebook.com/tools/explorer): pick the app, add the
     same six permissions, **Generate Access Token**, and in the dialog allow only the
     FlyFromTirana Page, business and Instagram account.
   - That token lives an hour. Click ⓘ next to it → Open in Access Token Tool →
     **Extend Access Token**; paste the extended token back into the Explorer and run
     `me/accounts?fields=name,access_token`. The `access_token` next to the Page is
     the permanent Page token.
   - Set it as `META_PAGE_TOKEN` in `.env` and in Coolify, then redeploy.
     `python scripts/meta_check.py` shows which Page and Instagram account it opens and
     how many of Instagram's 100 API posts a day are used. Posts start with the next run.

To make a post right now: `python scripts/social_test.py` scans today's prices and
sends the draft of the best deal of the moment to your chat (`--routes BGY,VIE` to
scan only those); `--weekly` makes the week's top deals and `--guide` the next city
guide instead, from the database as it is (no scan). On the server, `--publish` posts
it for real (Coolify: Terminal → `python scripts/social_test.py --publish`, or
`--weekly --publish`; a laptop can't, because Instagram fetches the pictures from the
site). Nothing is posted to the channel and no prices are saved, but the post is
remembered like a real one, so the bot doesn't repeat it; `--guide --skip` marks the
next city as posted without posting it (when you made that post by hand). To see the
cards without any deal:
`python scripts/social_preview.py` draws three sample deals, a top-5 cover and a guide card into
`ig_preview/`. A
`python -m src.main --dry-run` prints each caption and the folder its cards were drawn
into. The cards are drawn with Pillow and the fonts in `assets/fonts/` (the website
keeps loading the same fonts from Google Fonts). The city photos are the Telegram ones
(`assets/telegram/`); `bgy.jpg` is the smallest (600px wide), so Milan Bergamo's card is
a little softer than the others until that photo is replaced.

**The opening posts.** `content/intro_posts.yaml` holds nine posts that introduce the
accounts: a real €13 deal as the hook, this week's prices on four photos, how it works
in three big numbers, a mosaic of destinations, the Telegram channel on a phone,
Premium, the website on a phone, the guides, follow us. `src/intro_cards.py` draws
them; `python scripts/intro_posts.py` writes them into `ig_preview/intro/`, `--draft` sends them to your chat for a look, and on
the server `--publish` posts them in order (number 9 ends up top-left of the
Instagram grid; `--from N` carries on after a stop, `--only N` posts one). Edit the
YAML to change a card or a caption; `{TELEGRAM}`, `{WEBSITE}` and `{PREMIUM}` in a
caption become plain words on Instagram and links on Facebook.

Things to watch after the first automatic post: that Meta could fetch the pictures
through Cloudflare (the report says "could not take the picture" otherwise: then allow
`/social/` for Meta's crawler in Cloudflare's WAF), and Meta's limit of 100 Instagram
posts a day through the API, far above the 2 made here.

## Price alerts (the bot)

People set alerts in a private chat with the bot (`alerts.bot_username`,
[@FlyFromTiranaBot](https://t.me/FlyFromTiranaBot)): `/start` shows the cities as
buttons, a city shows the prices (`alerts.prices`: up to €20, €30, ... or "Çdo ofertë",
any deal), and the alert is saved. `/alerts` lists them with a delete button each,
`/stop` deletes them all. Each free-channel and Premium post has a "🎯 Më njofto kur bie
çmimi për Milan" link that opens the bot straight on that city's prices.

After every scan, `src/alerts.py` checks each alert against the city's current prices,
those the website shows: the cheapest flight at the alert's price or less, or a deal by
the free channel's rules for "any deal". A match goes to the person with the
destination's photo, the booking link (SubID `alerts`) and the channel's `bot` invite
link (`templates/sq_alert.txt`). Free alerts wait like the website: a deal Premium is
still getting first comes once the free channel may have it. **Premium members** (the
bot checks who is in the Premium channel) get theirs straight away, also from the
15-minute premium scans. A person hears about a city once per price band within
`repost_cooldown_days`, nothing goes out in quiet hours, and someone who blocks the bot
loses their alerts. At most `alerts.max_per_person` (10) alerts each.

The bot's chat (`src/bot.py`) runs in a thread of the scheduler, waiting on Telegram
for messages (long polling). It is the only reader of the bot's updates, so it also
hands the channel joins to the weekly report. Never run `python -m src.bot` or
`scripts/alert_chat.py` while the server runs: Telegram gives each update to whoever
asks first. The privacy page says what the bot keeps (the chat's number and the alerts).

### Your own trip watches

`trip_watches` in config.yaml are trips only you hear about, in your own chat
(`TELEGRAM_DRAFTS_CHAT_ID`, or else `TELEGRAM_ALERT_CHAT_ID`), e.g. Tirana ⇄ Memmingen,
3-4 days with a weekend, back by 24 December, up to €100 for both flights. After each
full scan `src/trips.py` asks the price API for the watch's dates (one-way prices both
ways, and return tickets: 3 calls a month), and writes when the cheapest trip that fits
is at `max_price` or less: the best three, each linked to its Aviasales search (no
affiliate marker: it's your booking). Then again only when it gets a price band cheaper,
or after `repost_cooldown_days`. Not in quiet hours; a watch whose dates have passed does
nothing, so you can leave it in. The destination needn't be a route: nothing about it goes
to the channels or the site. `python -m src.trips` shows what the watches find right now.

The API only has prices that people searched on Aviasales lately, so a quiet route like
Memmingen can have gaps; the closer the dates, the more prices there are.

## Growing the Telegram channel

Telegram shows a channel to almost no one by itself: it grows from the places that
advertise it. So each place has its own invite link (`invite_links` in config.yaml),
and the bot counts who joined through which one:

| Place | Its link goes |
|---|---|
| instagram | in the Instagram bio, **by hand** (the API can't edit the bio). Captions can't hold a link, so they say "t.me/flyfromtirana (linku në bio)", never "@flyfromtirana", which on Instagram is the Instagram account itself |
| facebook | in every Facebook post ("Ofertat dalin të parat në Telegram: …") |
| website | on every Telegram button of the site, in all three languages |
| whatsapp | at the end of the message a "Dërgoja një shoku në WhatsApp" link opens: under each free-channel post (not premium's: those members pay to have the deals first) and on every deal on the site (photo card, table row, city page) |
| bot | in the price alerts and the bot's messages |

The bot made the four links (`python scripts/invite_links.py` makes any missing one
and prints it for config.yaml). They show in the Telegram app with their join counts:
the channel → Manage channel → Invite links. A place left empty uses the public
address.

**The weekly report.** The bot's listener (above) stores the joins and leaves Telegram
reports (only the link's place and the time, never who it was), and after every full
run `python -m src.growth` notes both channels' member counts. On `growth.report_day` (Monday) after
`report_time`, your chat (TELEGRAM_DRAFTS_CHAT_ID, or else TELEGRAM_ALERT_CHAT_ID) gets:
the members of each channel and the change in 7 days, then where the free channel's
new members came from. `python -m src.growth --report` sends it now; `--dry-run` prints
it and saves nothing.

## Exit codes

`0` OK · `1` the run failed (API down, bad token, a post failed) · `2` configuration
problem (missing env var, bad config.yaml). The scheduler logs any non-zero code as an error.

## Roadmap (not built yet: where it plugs in)

- **Reels.** The weekly top and the city guides are built (see "Instagram and
  Facebook posts"); a short Reel (the card with a slow zoom, made with ffmpeg) is
  not. It needs ffmpeg in the scanner image, a public MP4 like the pictures, and
  Instagram's video container, which takes a minute to process. Music can't be
  added through the API. TikTok stays by hand: an app TikTok hasn't audited can
  only post privately.
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
