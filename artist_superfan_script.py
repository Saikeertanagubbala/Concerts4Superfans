import duckdb as dd
import numpy as np
import pandas as pd
import os
import time
import requests
import spotipy
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader
from datetime import date
from sklearn.preprocessing import MinMaxScaler
from spotipy.oauth2 import SpotifyOAuth

load_dotenv()

# ── Credentials ──────────────────────────────────────────────────────────────
CONSUMER_KEY = os.getenv('consumer_key')
app_password = os.getenv('app_password')
sender = os.getenv('gmail_account')
receiver = os.getenv('gmail_account')

# ── DuckDB Connection ─────────────────────────────────────────────────────────
conn = dd.connect('spotify_all_years.db')

# ── LAYER 1: Historical ETL + Feature Engineering ────────────────────────────
conn.execute(
    "CREATE TABLE complete_data AS "
    "SELECT * "
    "FROM read_json_auto('Spotify Extended Streaming History/*.json', union_by_name=true)"
)

conn.execute("CREATE TABLE clean_data AS SELECT ts,platform, ms_played, ip_addr,master_metadata_track_name,master_metadata_album_artist_name,master_metadata_album_album_name,spotify_track_uri,reason_start,reason_end,shuffle,skipped FROM complete_data")

conn.execute("CREATE TABLE podcast_dropped AS SELECT * FROM clean_data WHERE master_metadata_album_artist_name IS NOT NULL")

conn.execute("CREATE TABLE unique_songs_and_artists_per_artist AS SELECT master_metadata_album_artist_name, COUNT(DISTINCT master_metadata_album_album_name) AS unique_albums_for_artist, COUNT(DISTINCT master_metadata_track_name) AS num_unique_songs_for_artist FROM podcast_dropped GROUP BY master_metadata_album_artist_name")

conn.execute("CREATE TABLE non_single_albums_per_artist AS"
" SELECT master_metadata_album_artist_name, COUNT(album_name) AS num_true_albums"
" FROM (SELECT master_metadata_album_artist_name, master_metadata_album_album_name AS album_name, COUNT(DISTINCT master_metadata_track_name) AS num_tracks FROM podcast_dropped GROUP BY master_metadata_album_artist_name, album_name HAVING num_tracks > 1) AS album_counts"
" GROUP BY master_metadata_album_artist_name").df()

conn.execute("CREATE TABLE play_count AS SELECT COUNT(*) AS play_count_per_artist,master_metadata_album_artist_name"
" FROM podcast_dropped"
" GROUP BY master_metadata_album_artist_name").df()

conn.execute("CREATE TABLE listening_consistency AS SELECT master_metadata_album_artist_name, COUNT(*) AS survived_year_months"
" FROM (SELECT master_metadata_album_artist_name, strftime('%Y-%m', ts) AS year_month, COUNT(DISTINCT master_metadata_track_name) AS distinct_songs_listened"
" FROM podcast_dropped"
" GROUP BY master_metadata_album_artist_name, strftime('%Y-%m', ts)"
" HAVING distinct_songs_listened > 1) AS subquery"
" GROUP BY master_metadata_album_artist_name").df()

conn.execute("CREATE TABLE skip_rate AS SELECT master_metadata_album_artist_name, AVG(CAST(skipped AS INT)) as skip_rate"
" FROM podcast_dropped"
" GROUP BY master_metadata_album_artist_name").df()

conn.execute("CREATE TABLE intentional_listening_rate AS SELECT master_metadata_album_artist_name, SUM(CAST(reason_start IN ('clickrow', 'playbtn', 'backbtn') AS INT)) / COUNT(*) AS intentional_listening_rate"
" FROM podcast_dropped"
" GROUP BY master_metadata_album_artist_name").df()

conn.execute("CREATE TABLE completion_rate AS SELECT master_metadata_album_artist_name, SUM(CAST(reason_end = 'trackdone' AS INT)) / COUNT(*) AS completion_rate"
" FROM podcast_dropped"
" GROUP BY master_metadata_album_artist_name").df()

conn.execute("CREATE TABLE revisit_rate AS SELECT master_metadata_album_artist_name, COUNT(*) AS survived_revists_after_120"
" FROM (SELECT master_metadata_album_artist_name, ts, LAG(ts,1) OVER(PARTITION BY master_metadata_album_artist_name ORDER BY ts) AS prev_timestamp, DATEDIFF('day',prev_timestamp,ts) AS difference_days FROM podcast_dropped) AS all_revisits_difference"
" WHERE difference_days > 120"
" GROUP BY master_metadata_album_artist_name").df()

conn.execute("CREATE TABLE feature_engineered_table AS"
" SELECT p.master_metadata_album_artist_name, p.play_count_per_artist, u.unique_albums_for_artist, u.num_unique_songs_for_artist, c.completion_rate, i.intentional_listening_rate, s.skip_rate,l.survived_year_months AS listening_consistency,n.num_true_albums, r.survived_revists_after_120 AS revisit_rate"
" FROM play_count p"
" LEFT JOIN unique_songs_and_artists_per_artist u ON p.master_metadata_album_artist_name = u.master_metadata_album_artist_name"
" LEFT JOIN completion_rate c ON p.master_metadata_album_artist_name = c.master_metadata_album_artist_name"
" LEFT JOIN intentional_listening_rate i ON p.master_metadata_album_artist_name = i.master_metadata_album_artist_name"
" LEFT JOIN skip_rate s ON p.master_metadata_album_artist_name = s.master_metadata_album_artist_name"
" LEFT JOIN listening_consistency l ON p.master_metadata_album_artist_name = l.master_metadata_album_artist_name"
" LEFT JOIN non_single_albums_per_artist n ON p.master_metadata_album_artist_name=n.master_metadata_album_artist_name"
" LEFT JOIN revisit_rate r ON p.master_metadata_album_artist_name=r.master_metadata_album_artist_name")

df = conn.execute("SELECT * FROM feature_engineered_table").df()
df = df.drop(columns=['unique_albums_for_artist', 'skip_rate'])

df_with_log_variables = df.copy()
for i in df.select_dtypes(include='number').columns:
    df_with_log_variables['log_' + i] = np.log1p(df[i])

log_cols = [col for col in df_with_log_variables.columns if col.startswith('log_')]
log_cols.append('master_metadata_album_artist_name')
df_log_only = df_with_log_variables[log_cols]

df_numeric = df_log_only.select_dtypes(include='number')
scaler = MinMaxScaler()
scaled_data = scaler.fit_transform(df_numeric)
scaled_df = pd.DataFrame(scaled_data, columns=df_numeric.columns)

scaled_df['superfan_score'] = (
    0.30 * scaled_df['log_listening_consistency'] +
    0.15 * scaled_df['log_play_count_per_artist'] +
    0.15 * scaled_df['log_revisit_rate'] +
    0.15 * scaled_df['log_num_unique_songs_for_artist'] +
    0.10 * scaled_df['log_num_true_albums'] +
    0.10 * scaled_df['log_completion_rate'] +
    0.05 * scaled_df['log_intentional_listening_rate']
)
scaled_df['artist_name'] = df['master_metadata_album_artist_name']
scaled_df.sort_values('superfan_score', ascending=False)

conn.execute("CREATE TABLE superfan_scores AS "
             " SELECT *, ROW_NUMBER() OVER (ORDER BY superfan_score DESC) AS rank"
             " FROM scaled_df")

# ── LAYER 2: Spotify API — Live Listening Signal ──────────────────────────────
sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
    client_id=os.getenv('spotify_client_ID'),
    client_secret=os.getenv('spotify_client_secret'),
    redirect_uri="http://127.0.0.1:3000",
    scope="user-top-read"
))

def get_top_artists(time_period, offset=0):
    all_collected_artists = []
    while True:
        top_artists = sp.current_user_top_artists(limit=50, offset=offset, time_range=time_period)
        all_collected_artists += top_artists['items']
        if top_artists['next'] is None:
            break
        else:
            offset += 50
    return [artist['name'] for artist in all_collected_artists]

def get_top_tracks(time_period, offset=0):
    all_collected_artists_and_tracks = []
    while True:
        top_tracks = sp.current_user_top_tracks(limit=50, offset=offset, time_range=time_period)
        for track in top_tracks['items']:
            top_track_dict = {
                "artist_name": track['artists'][0]['name'],
                "track_name": track['name']
            }
            all_collected_artists_and_tracks.append(top_track_dict)
        if top_tracks['next'] is None:
            break
        else:
            offset += 50
    return all_collected_artists_and_tracks

long_term_artists = get_top_artists("long_term")
short_term_artists = get_top_artists("short_term")
medium_term_artists = get_top_artists("medium_term")
long_term_artists_and_tracks = get_top_tracks("long_term")

# Feature 1 — API presence score
total = len(long_term_artists)
api_presence = pd.DataFrame([
    {
        "artist_name": artist,
        "api_presence_score": (total - rank) / (total - 1)
    }
    for rank, artist in enumerate(long_term_artists, start=1)
])

# Feature 2 — Time range consistency
df_short = pd.DataFrame(short_term_artists, columns=["artist_name"])
df_medium = pd.DataFrame(medium_term_artists, columns=["artist_name"])
df_long = pd.DataFrame(long_term_artists, columns=["artist_name"])
df_short["in_short"] = 1
df_medium["in_medium"] = 1
df_long["in_long"] = 1
consistency = df_long.merge(df_medium, on="artist_name", how="outer") \
                     .merge(df_short, on="artist_name", how="outer") \
                     .fillna(0)
consistency["time_range_consistency"] = (consistency["in_short"] + consistency["in_medium"] + consistency["in_long"]) / 3

# Feature 3 — Track breadth
df_tracks = pd.DataFrame(long_term_artists_and_tracks)
track_breadth = df_tracks.groupby("artist_name")["track_name"].nunique().reset_index()
track_breadth.columns = ["artist_name", "track_breadth"]
track_breadth["track_breadth_score"] = track_breadth["track_breadth"] / track_breadth["track_breadth"].max()

# Blend historical + API features
df = conn.execute("SELECT * FROM superfan_scores").df()
df = df.merge(api_presence, on="artist_name", how="left")
df = df.merge(track_breadth[["artist_name", "track_breadth_score"]], on="artist_name", how="left")
df = df.merge(consistency[["artist_name", "time_range_consistency"]], on="artist_name", how="left")
df[["superfan_score", "api_presence_score", "time_range_consistency", "track_breadth_score"]] = \
    df[["superfan_score", "api_presence_score", "time_range_consistency", "track_breadth_score"]].fillna(0)
df["blended_score"] = (
    0.60 * df["superfan_score"] +
    0.20 * df["api_presence_score"] +
    0.10 * df["time_range_consistency"] +
    0.10 * df["track_breadth_score"]
)
df_final = df.sort_values("blended_score", ascending=False).head(50)
list_of_artists = df_final["artist_name"].tolist()

# ── LAYER 3: Ticketmaster — Concert Lookup ────────────────────────────────────
def get_event_details(query):
    base_url = "https://app.ticketmaster.com/discovery/v2/events.json"
    params = {
        "apikey": CONSUMER_KEY,
        "keyword": query,
        "classificationName": "music",
        "geoPoint": "dpsby4",   # geohash for Detroit
        "radius": 150,
        "unit": "miles",
        "size": 5,
        "countryCode": "US",
        "source": "ticketmaster"
    }
    response = requests.get(base_url, params=params)
    list_of_valid_artists = []
    if response.status_code == 200 and "_embedded" in response.json():
        for event in response.json()["_embedded"]["events"]:
            event_dict = {
                "artist_name": event['name'],
                "ticket_link": event['url'],
                "artist_image": max(event["images"], key=lambda x: x["width"])["url"],
                "concert_date": event['dates']['start']['localDate'],
                "venue": event['_embedded']['venues'][0]['name'],
                "city": event['_embedded']['venues'][0]['city']['name'],
                "address": event['_embedded']['venues'][0]['address']['line1'],
            }
            list_of_valid_artists.append(event_dict)
    elif response.status_code != 200:
        print(f"API error for {query}: {response.status_code}")
    else:
        return []
    return list_of_valid_artists

touring_artists = []
for i in list_of_artists:
    touring_artists += get_event_details(i)
    time.sleep(0.2)

# ── LAYER 4: Email Digest ─────────────────────────────────────────────────────
env = Environment(loader=FileSystemLoader("."))
template = env.get_template('email_template.html')
touring_artists = sorted(touring_artists, key=lambda x: x["concert_date"])
html_output = template.render(events=touring_artists, date=date.today().strftime("%B %d, %Y"))

msg = MIMEMultipart("alternative")
msg["Subject"] = "Your Weekly Concert Digest"
msg["From"] = sender
msg["To"] = receiver
msg.attach(MIMEText(html_output, "html"))

with smtplib.SMTP("smtp.gmail.com", 587) as server:
    server.starttls()
    server.login(sender, app_password)
    server.sendmail(sender, receiver, msg.as_string())
    print("Email sent!")