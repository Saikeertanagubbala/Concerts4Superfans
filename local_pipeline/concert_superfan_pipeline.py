import duckdb as dd
import numpy as np
import pandas as pd
import seaborn as sns
import matplotlib.pyplot as plt
import os
import time
import requests
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from scipy.stats import skew
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader
from datetime import date
load_dotenv()
conn = dd.connect('spotify_all_years.db')
TICKETMASTER_API_KEY = os.getenv('consumer_key')
# Appending all you history
conn.execute(
"CREATE TABLE complete_data AS " \
"SELECT * " \
"FROM read_json_auto('Spotify Extended Streaming History/*.json', union_by_name=true)"
)

# Cleaning
conn.execute("CREATE TABLE clean_data AS SELECT ts,platform, ms_played, ip_addr,master_metadata_track_name,master_metadata_album_artist_name,master_metadata_album_album_name,spotify_track_uri,reason_start,reason_end,shuffle,skipped FROM complete_data")

# Remove podcasts/episodes from entries.
conn.execute("CREATE TABLE podcast_dropped AS SELECT * FROM clean_data WHERE master_metadata_album_artist_name IS NOT NULL")

# Number of unique songs and unique albums listened to by artist (NOT accurate includes released singles as albums).
conn.execute("CREATE TABLE unique_songs_and_artists_per_artist AS SELECT master_metadata_album_artist_name, COUNT(DISTINCT master_metadata_album_album_name) AS unique_albums_for_artist, COUNT(DISTINCT master_metadata_track_name) AS num_unique_songs_for_artist FROM podcast_dropped GROUP BY master_metadata_album_artist_name")

# One row per artist, with a count of only the albums where you listened to more than 1 track. Non-single albums per artist.
conn.execute("CREATE TABLE non_single_albums_per_artist AS" \
" SELECT master_metadata_album_artist_name, COUNT(album_name) AS num_true_albums" \
" FROM (SELECT master_metadata_album_artist_name, master_metadata_album_album_name AS album_name, COUNT(DISTINCT master_metadata_track_name) AS num_tracks FROM podcast_dropped GROUP BY master_metadata_album_artist_name, album_name HAVING num_tracks > 1) AS album_counts" \
" GROUP BY master_metadata_album_artist_name").df()

# Total play count per artist
conn.execute("CREATE TABLE play_count AS SELECT COUNT(*) AS play_count_per_artist,master_metadata_album_artist_name"\
" FROM podcast_dropped" \
" GROUP BY master_metadata_album_artist_name").df()

# Listening consistency — COUNT(DISTINCT month/year) per artist where distinct songs listened to were > 1.
# artist_name | months_active (Jan2019, Feb2019, March2020 -> counted up but where songs were > 1)
conn.execute("CREATE TABLE listening_consistency AS SELECT master_metadata_album_artist_name, COUNT(*) AS survived_year_months" \
" FROM (SELECT master_metadata_album_artist_name, strftime('%Y-%m', ts) AS year_month, COUNT(DISTINCT master_metadata_track_name) AS distinct_songs_listened" \
" FROM podcast_dropped" \
" GROUP BY master_metadata_album_artist_name, strftime('%Y-%m', ts)" \
" HAVING distinct_songs_listened > 1) AS subquery" \
" GROUP BY master_metadata_album_artist_name").df()

# Skip rate per artist, higher = you skip the artist most of the time, lower = you let them play out.
conn.execute("CREATE TABLE skip_rate AS SELECT master_metadata_album_artist_name, AVG(CAST(skipped AS INT)) as skip_rate" \
" FROM podcast_dropped" \
" GROUP BY master_metadata_album_artist_name").df()

# Intentional listening rate: percentage of plays where reason_start is due to ('clickrow', 'playbtn', 'backbtn') per artist.
conn.execute("CREATE TABLE intentional_listening_rate AS SELECT master_metadata_album_artist_name, SUM(CAST(reason_start IN ('clickrow', 'playbtn', 'backbtn') AS INT)) / COUNT(*) AS intentional_listening_rate" \
" FROM podcast_dropped" \
" GROUP BY master_metadata_album_artist_name").df()

# Completion rate: where the reason it ends is because the track is done/ all plays per artist
conn.execute("CREATE TABLE completion_rate AS SELECT master_metadata_album_artist_name, SUM(CAST(reason_end = 'trackdone' AS INT)) / COUNT(*) AS completion_rate" \
" FROM podcast_dropped" \
" GROUP BY master_metadata_album_artist_name").df() 

# Revisit-rate: count of revisiting an artist over a 4 month gap.
conn.execute("CREATE TABLE revisit_rate AS SELECT master_metadata_album_artist_name, COUNT(*) AS survived_revists_after_120" \
" FROM (SELECT master_metadata_album_artist_name, ts, LAG(ts,1) OVER(PARTITION BY master_metadata_album_artist_name ORDER BY ts) AS prev_timestamp, DATEDIFF('day',prev_timestamp,ts) AS difference_days FROM podcast_dropped) AS all_revisits_difference" \
" WHERE difference_days > 120" \
" GROUP BY master_metadata_album_artist_name").df()

# Final feature engineered tabble
conn.execute("CREATE TABLE feature_engineered_table AS" \
" SELECT p.master_metadata_album_artist_name, p.play_count_per_artist, u.unique_albums_for_artist, u.num_unique_songs_for_artist, c.completion_rate, i.intentional_listening_rate, s.skip_rate,l.survived_year_months AS listening_consistency,n.num_true_albums, r.survived_revists_after_120 AS revisit_rate" \
" FROM play_count p" \
" LEFT JOIN unique_songs_and_artists_per_artist u ON p.master_metadata_album_artist_name = u.master_metadata_album_artist_name" \
" LEFT JOIN completion_rate c ON p.master_metadata_album_artist_name = c.master_metadata_album_artist_name" \
" LEFT JOIN intentional_listening_rate i ON p.master_metadata_album_artist_name = i.master_metadata_album_artist_name" \
" LEFT JOIN skip_rate s ON p.master_metadata_album_artist_name = s.master_metadata_album_artist_name" \
" LEFT JOIN listening_consistency l ON p.master_metadata_album_artist_name = l.master_metadata_album_artist_name" \
" LEFT JOIN non_single_albums_per_artist n ON p.master_metadata_album_artist_name=n.master_metadata_album_artist_name" \
" LEFT JOIN revisit_rate r ON p.master_metadata_album_artist_name=r.master_metadata_album_artist_name")

# Dropped columns after EDA exploration: multi-collinearty issues
df = conn.execute("SELECT * FROM feature_engineered_table").df()
df = df.drop(columns=['unique_albums_for_artist','skip_rate'])

# Log transformed variables -> min-max scale -> build score
df_with_log_variables = df.copy() #make copy of original df
for i in df.select_dtypes(include='number').columns:
    df_with_log_variables['log_' + i] = np.log1p(df[i]) #appended new log columns

log_cols = [col for col in df_with_log_variables.columns if col.startswith('log_')] #only log columns
log_cols.append('master_metadata_album_artist_name') # addding back artist names
df_log_only = df_with_log_variables[log_cols] #log columns + artists = new df
from sklearn.preprocessing import MinMaxScaler
df_numeric = df_log_only.select_dtypes(include='number')
scaler = MinMaxScaler()
scaled_data = scaler.fit_transform(df_numeric)
scaled_df = pd.DataFrame(scaled_data, columns=df_numeric.columns) #new scaled df based on log transformed df

# Creating a score for each artists scaled by the variables: chosen via domain knowledge
scaled_df['superfan_score'] = (
    0.30 * scaled_df['log_listening_consistency'] +
    0.15 * scaled_df['log_play_count_per_artist'] +
    0.15 * scaled_df['log_revisit_rate'] +
    0.15 * scaled_df['log_num_unique_songs_for_artist'] +
    0.10 * scaled_df['log_num_true_albums'] +
    0.10 * scaled_df['log_completion_rate'] +
    0.05 * scaled_df['log_intentional_listening_rate']
)
scaled_df['artist_name'] = df['master_metadata_album_artist_name'] #appended artist name colummn back
scaled_df.sort_values('superfan_score', ascending=False) #sorting by highest -> lowest scores for artists

#export with new column
conn.execute("CREATE TABLE superfan_scores AS " \
" SELECT *, ROW_NUMBER() OVER (ORDER BY superfan_score DESC) AS rank" \
" FROM scaled_df")

# Getting list of events + their details via ticketmaster api
def get_event_details(query):
    base_url = "https://app.ticketmaster.com/discovery/v2/events.json"
    params = {
        "apikey": TICKETMASTER_API_KEY,
        "keyword": query,
        "classificationName": "music",  
        "geoPoint": "dpsby4",   #geohash for detroit            
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
                "concert_date": event['dates']['start']['localDate'] ,
                "venue":event['_embedded']['venues'][0]['name'] ,
                "city": event['_embedded']['venues'][0]['city']['name'],
                "address":event['_embedded']['venues'][0]['address']['line1'] ,
            }
            list_of_valid_artists.append(event_dict)
    elif response.status_code != 200:
        print(f"API error for {query}: {response.status_code}")
    else: 
        return []
    
    return list_of_valid_artists

# Passing in the top 50 artists from the scored table into the ticketmaster api function
top_50 = conn.execute("SELECT * FROM superfan_scores").df().head(50)
list_of_artists = top_50["artist_name"].tolist()
touring_artists = []
for i in list_of_artists:
    touring_artists += get_event_details(i)
    time.sleep(0.2)

# Rendering email using html template 
env = Environment(loader=FileSystemLoader("."))
template = env.get_template('email_template.html')
touring_artists = sorted(touring_artists, key=lambda x: x["concert_date"]) # making the output chronologically ordered
html_output = template.render(events = touring_artists, date = date.today().strftime("%B %d, %Y"))

# Sending email using the html that is generated for an event an artist has 
app_password = os.getenv('app_password')
sender = os.getenv('gmail_account')
receiver = os.getenv('gmail_account')

msg = MIMEMultipart("alternative")
msg["Subject"] = "Your Weekly Concert Digest"
msg["From"] = sender
msg["To"] = receiver
msg.attach(MIMEText(html_output, "html"))

with smtplib.SMTP("smtp.gmail.com", 587) as server:
    server.starttls()
    server.login(sender, app_password)
    server.sendmail(sender, receiver, msg.as_string())