import pandas as pd
import os
import time
import requests
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader
from datetime import date

load_dotenv()
CONSUMER_KEY = os.getenv('CONSUMER_KEY')


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


# Load top 50 artists from dummy data CSV
top_50 = pd.read_csv("billboard_top_artists_sample_data.csv")
list_of_artists = top_50["artist_name"].tolist()

# Query Ticketmaster for each artist
touring_artists = []
for artist in list_of_artists:
    touring_artists += get_event_details(artist)
    time.sleep(0.2)

# Sort chronologically
touring_artists = sorted(touring_artists, key=lambda x: x["concert_date"])

# Render HTML email template
env = Environment(loader=FileSystemLoader("."))
template = env.get_template('email_template.html')
html_output = template.render(events=touring_artists, date=date.today().strftime("%B %d, %Y"))

# Send email
app_password = os.getenv('APP_PASSWORD')
sender = os.getenv('GMAIL_ACCOUNT')
receiver = os.getenv('GMAIL_ACCOUNT')

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