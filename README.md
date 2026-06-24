# Concerts4Superfans

A personalized concert recommendation tool that analyzes your Extended Spotify listening history to send upcoming shows for artists you actually love and intentionally listen to, not just one hit wonders.

---

## What It Does

Most concert recommendation tools are based on what's popular or what you've recently streamed and are easy to skew if you listen to a particular album or song. Concerts4Superfans goes deeper and builds a **superfan score** for every artist in your Spotify history using behavioral signals like discography breadth, listening consistency, revisit rate, and completion rate. It then layers on live Spotify API signals (mainly from the past year) to capture who you've been loving recently to prevent staleness of data, and queries Ticketmaster for upcoming shows near you. The result is a styled HTML email digest delivered to your inbox on a weekly schedule. Ultimately it allows you to spend money for an artist that you genuinely care about without decision fatigue.

---

## How It Works

### Layer 1 — Historical ETL + Feature Engineering
Your Spotify Extended Streaming History (years of JSON data) is loaded into DuckDB via a full ETL pipeline. Seven features are engineered per artist using SQL:

| Feature | What It Measures |
|---|---|
| Play count | Total play count per artist |
| Unique song count | How broadly you explored an artist's discography |
| Listening consistency | Months where you listened to more than one distinct track |
| True album count | Albums where you listened to more than one track (filters out singles) |
| Revisit rate | How many times you returned after a 120+ day gap |
| Completion rate | Fraction of plays where the track played to the end |
| Intentional listening rate | Fraction of plays triggered by a deliberate action (click, play button, replay) |

Features are log-transformed, min-max scaled, and combined into a weighted **superfan score**. Weights were informed by domain knowlege and can be changed. Also performed K-Means clustering and Kruskal-Wallis statistical validation to ensure each feature meaningfully differentiates superfan behavior from casual listening but weren't used.

### Layer 2 — Spotify API Live Signal
To capture who you've been loving *recently* — not just historically — the pipeline pulls three additional features from the Spotify Web API:

| Feature | Source | What It Measures |
|---|---|---|
| Presence score | Long-term (approx. 1 year) top artists | Rank-weighted signal of current Spotify engagement |
| Time range consistency | Short + medium + long term top artists | Appeared across all three windows = sustained obsession |
| Track breadth | Long-term top tracks | Distinct songs from that artist in your top tracks |

These three features are blended with the historical superfan score (60/20/10/10 weighting) to produce a final **blended score** that balances depth of fandom from extended listening history with recency of listening from the API.

### Layer 3 — Ticketmaster Concert Lookup
The top 50 artists by blended score are queried against the Ticketmaster Discovery API, filtered to:
- Music events only
- Fface value tickets only (no resale)
- Within 150 miles of your location (Change GeoHash to your specific city in the script using http://geohash.co/)


### Layer 4 — Email Digest
Matching events are rendered into a styled HTML email using a template and sent via Gmail's SMTP server. Events are sorted chronologically. The email includes the artist image, event name, date, venue, city, address, and a direct ticket purchase link.

![Concert Digest Email Preview](screenshots/Screenshot%202026-06-24%20145837.png)
![Billboard Preview](screenshots/Screenshot%202026-06-24%20145957.png)

---

## Repo Structure

```
concert-superfan/
├── artist_superfan_script.py       # End-to-end pipeline script
├── email_template.html  # Jinja2 HTML email template
│
├── pipeline_demo/           # Demo pipeline (runs via GitHub Actions on Billboard data)
│   ├── consolidated_python_script.py
│   ├── email_template.html
│   └── billboard_top_artists_sample_data.csv
|   └── dummy_data.ipynb     # Was used to create the sample csv
│
├── exploration/             # Notebooks documenting intermediate analysis steps
│   ├── cleaning_feature_engineering.ipynb
│   ├── eda_transformation_superfan_scorer.ipynb
│   └── ticketmaster_api.ipynb
|   └── API_layer_on_extended_history.ipynb
│
└── .github/
    └── workflows/
        ├── concert_digest.yml        # Demo pipeline — runs weekly
        ├── full_pipeline.yml         # Full pipeline — runs weekly (requires setup)
        └── data_refresh_reminder.yml # 6-month reminder to update Spotify data
```

---

## Running the Full Pipeline (Local)

### Prerequisites
- Python 3.12+
- Your Spotify Extended Streaming History JSON files (request from [spotify.com/account/privacy](https://www.spotify.com/account/privacy))
- Ticketmaster Developer API key ([developer.ticketmaster.com](https://developer.ticketmaster.com))
- Spotify Developer app credentials ([developer.spotify.com](https://developer.spotify.com))
- Gmail account with an App Password enabled ([apppasswords]https://myaccount.google.com/apppasswords)

### Setup

1. Clone the repo and install dependencies:
```bash
pip install duckdb pandas numpy scikit-learn spotipy requests jinja2 python-dotenv
```

2. Place your Spotify JSON files in a folder called `Spotify Extended Streaming History/`

3. Create a `.env` file with the following:
```
consumer_key=YOUR_TICKETMASTER_KEY
spotify_client_ID=YOUR_SPOTIFY_CLIENT_ID
spotify_client_secret=YOUR_SPOTIFY_CLIENT_SECRET
gmail_account=YOUR_GMAIL_ADDRESS
app_password=YOUR_GMAIL_APP_PASSWORD
```

4. Update the `geoPoint` parameter in `full_script.py` with the geohash for your location. Find yours at [geohash.co](http://geohash.co/)

5. Run the pipeline:
```bash
python full_script.py
```

On first run, a browser window will open asking you to authorize Spotify access. Approve it — a `.cache` token file will be saved automatically and used for all future runs.

---

## Running the Demo Pipeline (GitHub Actions)

The demo pipeline uses Billboard Hot 100 artist data instead of personal listening history, so no Spotify data is required. It runs automatically every Sunday via GitHub Actions.

### Setup

1. Fork the repo
2. Add the following as GitHub repository secrets (Settings -> Secrets -> Actions):
   - `CONSUMER_KEY` — Ticketmaster API key
   - `GMAIL_ACCOUNT` — your Gmail address
   - `APP_PASSWORD` — your Gmail App Password
3. Update `geoPoint` in `pipeline_demo/consolidated_python_script.py` with your location geohash
4. The workflow in `.github/workflows/concert_digest.yml` will run automatically every Sunday at 9am EST

To trigger it manually: go to Actions -> Weekly Concert Digest -> Run workflow.

---

## Automating the Full Pipeline via GitHub Actions

To run the full personalized pipeline (with your own scores) on a schedule:

1. Complete the local setup above and run the script once to generate the `.cache` file
2. Copy the entire contents of `.cache` and add it as a GitHub secret called `SPOTIFY_TOKEN_CACHE`
3. Add all remaining secrets: `CONSUMER_KEY`, `GMAIL_ACCOUNT`, `APP_PASSWORD`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`
4. Move `.github/workflows/full_pipeline.yml` to replace `concert_digest.yml`, or add it alongside — both run independently

---

## Data Refresh

Your Spotify Extended Streaming History captures data up to when you requested the export. A scheduled GitHub Action runs every January 1st and July 1st to remind you to download a fresh export and update your local data.

To request a new export: [spotify.com/account/privacy](https://www.spotify.com/account/privacy) -> Download your data -> Extended Streaming History (allow up to 30 days for delivery).

---

## Known Limitations

- **Fuzzy artist matching:** Ticketmaster's `keyword` search is not exact — occasionally returns events for artists with similar names. Post-filtering is a planned improvement.
- **50-artist API cap:** Spotify's top artists/tracks endpoints return a maximum of 50 results per time range. Pagination is implemented but Spotify enforces a hard ceiling on personal data endpoints.
- **OAuth for automation:** The Spotify OAuth flow requires a one-time browser login. The `.cache` token handles subsequent automated runs but must be refreshed if access is revoked. Best to run it locally than via GitHub.
- **Scoring logic:** Weighs your historical listening data at 60% and your recent plays at 40%, can be customized to your liking. I found that this provided an accurate mix of new and old. 

---

## Tech Stack

`Python` · `DuckDB` · `SQL` · `pandas` · `scikit-learn` · `spotipy` · `Ticketmaster Discovery API` · `Spotify Web API` · `Jinja2` · `smtplib` · `GitHub Actions`