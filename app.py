"""
WeatherSphere AI - Production-style Flask weather intelligence platform.

Data source: Open-Meteo (free, keyless) — geocoding, forecast, air quality
and historical archive endpoints. No API key required, so the project
runs out of the box.
"""

import io
import json
import sqlite3
import uuid
from datetime import datetime, timedelta

import requests
from flask import (
    Flask, render_template, request, jsonify, g, redirect,
    url_for, make_response, send_file
)

APP_NAME = "WeatherSphere AI"
DB_PATH = "database/weathersphere.db"

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

# [WMO weather code -> (label, icon class, css state) lookup]

WEATHER_CODES = {
    0: ("Clear sky", "fa-sun", "clear"),
    1: ("Mainly clear", "fa-cloud-sun", "clear"),
    2: ("Partly cloudy", "fa-cloud-sun", "cloudy"),
    3: ("Overcast", "fa-cloud", "cloudy"),
    45: ("Fog", "fa-smog", "fog"),
    48: ("Rime fog", "fa-smog", "fog"),
    51: ("Light drizzle", "fa-cloud-rain", "rain"),
    53: ("Moderate drizzle", "fa-cloud-rain", "rain"),
    55: ("Dense drizzle", "fa-cloud-rain", "rain"),
    56: ("Freezing drizzle", "fa-cloud-meatball", "rain"),
    57: ("Dense freezing drizzle", "fa-cloud-meatball", "rain"),
    61: ("Slight rain", "fa-cloud-rain", "rain"),
    63: ("Moderate rain", "fa-cloud-showers-heavy", "rain"),
    65: ("Heavy rain", "fa-cloud-showers-heavy", "rain"),
    66: ("Freezing rain", "fa-cloud-meatball", "rain"),
    67: ("Heavy freezing rain", "fa-cloud-meatball", "rain"),
    71: ("Slight snow", "fa-snowflake", "snow"),
    73: ("Moderate snow", "fa-snowflake", "snow"),
    75: ("Heavy snow", "fa-snowflake", "snow"),
    77: ("Snow grains", "fa-snowflake", "snow"),
    80: ("Slight showers", "fa-cloud-rain", "rain"),
    81: ("Moderate showers", "fa-cloud-showers-heavy", "rain"),
    82: ("Violent showers", "fa-cloud-showers-heavy", "storm"),
    85: ("Slight snow showers", "fa-snowflake", "snow"),
    86: ("Heavy snow showers", "fa-snowflake", "snow"),
    95: ("Thunderstorm", "fa-bolt", "storm"),
    96: ("Thunderstorm w/ hail", "fa-bolt", "storm"),
    99: ("Severe thunderstorm w/ hail", "fa-bolt", "storm"),
}


def weather_info(code):
    return WEATHER_CODES.get(int(code), ("Unknown", "fa-question", "cloudy"))


# [Database]

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT UNIQUE NOT NULL,
            display_name TEXT DEFAULT 'Guest Explorer',
            unit TEXT DEFAULT 'celsius',
            theme TEXT DEFAULT 'dark',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS search_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_uuid TEXT NOT NULL,
            city TEXT NOT NULL,
            country TEXT,
            lat REAL,
            lon REAL,
            searched_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS favorite_cities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_uuid TEXT NOT NULL,
            city TEXT NOT NULL,
            country TEXT,
            lat REAL,
            lon REAL,
            added_at TEXT NOT NULL,
            UNIQUE(user_uuid, city, country)
        );

        CREATE TABLE IF NOT EXISTS weather_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            city TEXT NOT NULL,
            alert_type TEXT NOT NULL,
            message TEXT NOT NULL,
            severity TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS weather_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            city TEXT NOT NULL,
            country TEXT,
            report_date TEXT NOT NULL,
            data_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


def ensure_user(uuid_str):
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE uuid = ?", (uuid_str,)).fetchone()
    if row is None:
        db.execute(
            "INSERT INTO users (uuid, created_at) VALUES (?, ?)",
            (uuid_str, datetime.utcnow().isoformat()),
        )
        db.commit()
        row = db.execute("SELECT * FROM users WHERE uuid = ?", (uuid_str,)).fetchone()
    return row


@app.before_request
def attach_user():
    g.user_uuid = request.cookies.get("ws_uid")


@app.after_request
def attach_cookie(resp):
    if not request.cookies.get("ws_uid") and getattr(g, "new_uid", None):
        resp.set_cookie("ws_uid", g.new_uid, max_age=60 * 60 * 24 * 365)
    return resp


def current_user_uuid():
    uid = request.cookies.get("ws_uid")
    if not uid:
        uid = str(uuid.uuid4())
        g.new_uid = uid
    ensure_user(uid)
    return uid

# [External API helpers]

def geocode_city(name, count=1):
    try:
        resp = requests.get(
            GEOCODE_URL, params={"name": name, "count": count, "language": "en", "format": "json"},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("results", [])
    except requests.RequestException:
        return []


def fetch_forecast(lat, lon):
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": ",".join([
            "temperature_2m", "relative_humidity_2m", "apparent_temperature",
            "is_day", "precipitation", "weather_code", "cloud_cover",
            "pressure_msl", "wind_speed_10m", "wind_direction_10m",
            "wind_gusts_10m", "visibility",
        ]),
        "hourly": ",".join([
            "temperature_2m", "relative_humidity_2m", "precipitation_probability",
            "weather_code", "wind_speed_10m", "uv_index",
        ]),
        "daily": ",".join([
            "weather_code", "temperature_2m_max", "temperature_2m_min",
            "precipitation_probability_max", "uv_index_max", "sunrise",
            "sunset", "wind_speed_10m_max",
        ]),
        "timezone": "auto",
        "forecast_days": 7,
    }
    resp = requests.get(FORECAST_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_air_quality(lat, lon):
    try:
        resp = requests.get(
            AIR_QUALITY_URL,
            params={
                "latitude": lat, "longitude": lon,
                "current": "us_aqi,pm2_5,pm10,ozone,nitrogen_dioxide,carbon_monoxide",
            },
            timeout=8,
        )
        resp.raise_for_status()
        return resp.json().get("current", {})
    except requests.RequestException:
        return {}


def fetch_history(lat, lon, days=7):
    end = datetime.utcnow().date() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    try:
        resp = requests.get(
            ARCHIVE_URL,
            params={
                "latitude": lat, "longitude": lon,
                "start_date": start.isoformat(), "end_date": end.isoformat(),
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max",
                "timezone": "auto",
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("daily", {})
    except requests.RequestException:
        return {}


def aqi_category(aqi):
    if aqi is None:
        return "Unknown", "unknown"
    if aqi <= 50:
        return "Good", "good"
    if aqi <= 100:
        return "Moderate", "moderate"
    if aqi <= 150:
        return "Unhealthy (Sensitive)", "sensitive"
    if aqi <= 200:
        return "Unhealthy", "unhealthy"
    if aqi <= 300:
        return "Very Unhealthy", "very-unhealthy"
    return "Hazardous", "hazardous"

# [Rule-based "AI" weather intelligence engine]
def build_ai_insights(city, current, daily, aqi_current):
    temp = current.get("temperature_2m", 0)
    feels = current.get("apparent_temperature", temp)
    humidity = current.get("relative_humidity_2m", 0)
    wind = current.get("wind_speed_10m", 0)
    code = current.get("weather_code", 0)
    precip_prob = 0
    uv_max = 0
    if daily.get("precipitation_probability_max"):
        precip_prob = daily["precipitation_probability_max"][0]
    if daily.get("uv_index_max"):
        uv_max = daily["uv_index_max"][0]
    aqi = aqi_current.get("us_aqi")
    label, _, _ = weather_info(code)

    #  Weather summary 
    temp_desc = (
        "scorching" if temp >= 38 else
        "hot" if temp >= 32 else
        "warm" if temp >= 24 else
        "mild" if temp >= 16 else
        "cool" if temp >= 8 else "cold"
    )
    humidity_desc = "high humidity" if humidity >= 70 else "moderate humidity" if humidity >= 40 else "low humidity"
    summary = (
        f"{city} is currently {label.lower()} and {temp_desc}, with {humidity_desc} "
        f"({humidity}%) and a feels-like temperature of {round(feels)}°C. "
    )
    if precip_prob >= 60:
        summary += "Rain is likely later today, so keep an eye on the sky."
    elif precip_prob >= 30:
        summary += "There's a moderate chance of rain, so a light rain plan is worth having."
    else:
        summary += "Conditions look dry and settled for most of the day."

    #  Outfit recommendation 
    outfit = []
    if temp >= 32:
        outfit.append("Wear light, breathable cotton clothing")
        outfit.append("Choose light colors that reflect heat")
    elif temp >= 20:
        outfit.append("A t-shirt or light layers will feel comfortable")
    elif temp >= 10:
        outfit.append("Carry a light jacket or sweater for the cooler hours")
    else:
        outfit.append("Bundle up with a heavy coat, gloves, and a scarf")
    if precip_prob >= 40:
        outfit.append("Carry an umbrella or a waterproof jacket")
    if uv_max >= 6:
        outfit.append("Wear sunglasses and apply sunscreen before heading out")
    if wind >= 30:
        outfit.append("Wind-resistant outerwear is recommended today")

    #  Travel recommendation
    if code in (95, 96, 99):
        travel = "Postpone non-essential travel — thunderstorms are expected in the area."
    elif precip_prob >= 70:
        travel = "Avoid extended outdoor travel; heavy rainfall is likely."
    elif wind >= 40:
        travel = "Drive carefully — strong wind gusts may affect visibility and stability."
    elif code in (0, 1, 2) and temp < 34:
        travel = "Great day for sightseeing, road trips, or outdoor commutes."
    else:
        travel = "Travel conditions are manageable; plan around the forecast timeline."

    #  Health advisory 
    health = []
    if aqi is not None and aqi > 150:
        health.append("High pollution detected — sensitive groups should limit outdoor exposure.")
    elif aqi is not None and aqi > 100:
        health.append("Air quality is moderate to unhealthy for sensitive groups; consider a mask outdoors.")
    if temp >= 38:
        health.append("Heatwave conditions — stay hydrated and avoid peak-sun hours (12–4 PM).")
    if humidity >= 80 and temp >= 30:
        health.append("High heat combined with humidity increases heatstroke risk — take breaks in shade.")
    if temp <= 5:
        health.append("Cold exposure risk — limit time outdoors and dress in layers.")
    if not health:
        health.append("No significant health concerns detected for current conditions.")

    #  Activity suggestions 
    if code in (0, 1) and 15 <= temp <= 30 and wind < 25:
        activity = "Ideal conditions for jogging, cycling, or outdoor sports."
    elif code in (2, 3) and precip_prob < 40:
        activity = "Decent day for a walk or light outdoor activity."
    elif precip_prob >= 60 or code in (95, 96, 99):
        activity = "Indoor activities are recommended — save the outdoor plans for a clearer day."
    else:
        activity = "Moderate outdoor activity is fine; stay alert to changing conditions."

    #  Risk prediction (0-100 scale) 
    rain_risk = min(100, int(precip_prob))
    storm_risk = 90 if code in (95, 96, 99) else (45 if precip_prob >= 70 else 15 if precip_prob >= 40 else 5)
    heatwave_risk = min(100, max(0, int((temp - 25) * 6))) if temp > 25 else 0

    return {
        "summary": summary,
        "outfit": outfit,
        "travel": travel,
        "health": health,
        "activity": activity,
        "risks": {
            "rain": rain_risk,
            "storm": storm_risk,
            "heatwave": heatwave_risk,
        },
    }


def build_alerts(city, current, daily, aqi_current):
    alerts = []
    code = current.get("weather_code", 0)
    wind = current.get("wind_speed_10m", 0)
    temp = current.get("temperature_2m", 0)
    precip_prob = daily.get("precipitation_probability_max", [0])[0] if daily.get("precipitation_probability_max") else 0
    aqi = aqi_current.get("us_aqi")

    if code in (95, 96, 99):
        alerts.append(("Storm", "Thunderstorm activity detected in the region.", "high"))
    if precip_prob >= 70:
        alerts.append(("Rain", "Heavy rainfall expected — flooding possible in low-lying areas.", "medium"))
    if temp >= 40:
        alerts.append(("Heatwave", "Extreme heat warning — avoid prolonged sun exposure.", "high"))
    elif temp >= 36:
        alerts.append(("Heatwave", "Heatwave conditions building — stay hydrated.", "medium"))
    if wind >= 50:
        alerts.append(("Wind", "Strong wind gusts may cause disruption.", "medium"))
    if aqi is not None and aqi > 150:
        alerts.append(("Air Quality", f"Poor air quality (AQI {int(aqi)}) — limit outdoor exertion.", "high"))
    return alerts

# [Core data assembly for a city]
def build_city_package(city_query):
    # Support raw "lat,lon" queries produced by the geolocation button.
    coord_place = None
    parts = city_query.split(",")
    if len(parts) == 2:  
        try:
            lat_val, lon_val = float(parts[0]), float(parts[1])
            coord_place = {
                "name": "Current Location", "country": "", "admin1": "",
                "latitude": lat_val, "longitude": lon_val, "timezone": "",
            }
        except ValueError:
            coord_place = None

    if coord_place:
        place = coord_place
    else:
        matches = geocode_city(city_query, count=1)
        if not matches:
            return None
        place = matches[0]

    lat, lon = place["latitude"], place["longitude"]
    forecast = fetch_forecast(lat, lon)
    aqi_data = fetch_air_quality(lat, lon)
    current = forecast.get("current", {})
    daily = forecast.get("daily", {})
    hourly = forecast.get("hourly", {})

    hourly_times = hourly.get("time", [])

    label, icon, state = weather_info(current.get("weather_code", 0))
    aqi_val = aqi_data.get("us_aqi")
    aqi_label, aqi_class = aqi_category(aqi_val)

    ai = build_ai_insights(place["name"], current, daily, aqi_data)
    alerts = build_alerts(place["name"], current, daily, aqi_data)

    return {
        "place": place,
        "current": current,
        "daily": daily,
        "hourly": hourly,
        "condition_label": label,
        "condition_icon": icon,
        "condition_state": state,
        "aqi": aqi_val,
        "aqi_label": aqi_label,
        "aqi_class": aqi_class,
        "ai": ai,
        "alerts": alerts,
    }

# [Routes — pages]

@app.route("/")
def index():
    trending = ["New Delhi", "Mumbai", "London", "New York", "Tokyo", "Dubai", "Singapore", "Sydney"]
    return render_template("index.html", app_name=APP_NAME, trending=trending)


@app.route("/weather/<city>")
def weather_detail(city):
    uid = current_user_uuid()
    package = build_city_package(city)
    if package is None:
        return render_template("index.html", app_name=APP_NAME, trending=[
            "New Delhi", "Mumbai", "London", "New York", "Tokyo", "Dubai"
        ], error=f"We couldn't find a place named '{city}'. Try another spelling.")

    db = get_db()
    place = package["place"]
    db.execute(
        "INSERT INTO search_history (user_uuid, city, country, lat, lon, searched_at) VALUES (?,?,?,?,?,?)",
        (uid, place["name"], place.get("country", ""), place["latitude"], place["longitude"], datetime.utcnow().isoformat()),
    )
    db.execute(
        "INSERT INTO weather_reports (city, country, report_date, data_json, created_at) VALUES (?,?,?,?,?)",
        (place["name"], place.get("country", ""), datetime.utcnow().date().isoformat(),
         json.dumps({"current": package["current"], "aqi": package["aqi"]}), datetime.utcnow().isoformat()),
    )
    for alert_type, message, severity in package["alerts"]:
        db.execute(
            "INSERT INTO weather_alerts (city, alert_type, message, severity, created_at) VALUES (?,?,?,?,?)",
            (place["name"], alert_type, message, severity, datetime.utcnow().isoformat()),
        )
    db.commit()

    is_fav = db.execute(
        "SELECT 1 FROM favorite_cities WHERE user_uuid=? AND city=?", (uid, place["name"])
    ).fetchone() is not None

    return render_template(
        "weather.html", app_name=APP_NAME, city=place["name"], is_favorite=is_fav, **package
    )


@app.route("/dashboard")
def dashboard():
    uid = current_user_uuid()
    db = get_db()
    history = db.execute(
        "SELECT * FROM search_history WHERE user_uuid=? ORDER BY searched_at DESC LIMIT 20", (uid,)
    ).fetchall()
    favorites = db.execute(
        "SELECT * FROM favorite_cities WHERE user_uuid=? ORDER BY added_at DESC", (uid,)
    ).fetchall()
    total_searches = db.execute(
        "SELECT COUNT(*) c FROM search_history WHERE user_uuid=?", (uid,)
    ).fetchone()["c"]
    top_city_row = db.execute(
        """SELECT city, COUNT(*) c FROM search_history WHERE user_uuid=?
           GROUP BY city ORDER BY c DESC LIMIT 1""", (uid,)
    ).fetchone()
    top_city = top_city_row["city"] if top_city_row else "—"

    # live snapshot for favorites (small dashboard widgets)
    favorite_snapshots = []
    for fav in favorites:
        try:
            forecast = fetch_forecast(fav["lat"], fav["lon"])
            cur = forecast.get("current", {})
            label, icon, state = weather_info(cur.get("weather_code", 0))
            favorite_snapshots.append({
                "city": fav["city"], "country": fav["country"],
                "temp": cur.get("temperature_2m"), "label": label, "icon": icon, "state": state,
            })
        except Exception:
            continue

    return render_template(
        "dashboard.html", app_name=APP_NAME, history=history, favorites=favorites,
        total_searches=total_searches, top_city=top_city, favorite_snapshots=favorite_snapshots,
    )

@app.route("/analytics")
@app.route("/analytics/<city>")
def analytics(city=None):
    uid = current_user_uuid()
    db = get_db()
    if city is None:
        last = db.execute(
            "SELECT city FROM search_history WHERE user_uuid=? ORDER BY searched_at DESC LIMIT 1", (uid,)
        ).fetchone()
        city = last["city"] if last else "New Delhi"

    matches = geocode_city(city, count=1)
    chart_data = None
    place = None
    if matches:
        place = matches[0]
        history_daily = fetch_history(place["latitude"], place["longitude"], days=7)
        forecast = fetch_forecast(place["latitude"], place["longitude"])
        aqi_data = fetch_air_quality(place["latitude"], place["longitude"])
        daily_forecast = forecast.get("daily", {})

        chart_data = {
            "past_dates": history_daily.get("time", []),
            "past_max": history_daily.get("temperature_2m_max", []),
            "past_min": history_daily.get("temperature_2m_min", []),
            "past_wind": history_daily.get("wind_speed_10m_max", []),
            "past_precip": history_daily.get("precipitation_sum", []),
            "future_dates": daily_forecast.get("time", []),
            "future_max": daily_forecast.get("temperature_2m_max", []),
            "future_min": daily_forecast.get("temperature_2m_min", []),
            "future_uv": daily_forecast.get("uv_index_max", []),
            "future_rain_prob": daily_forecast.get("precipitation_probability_max", []),
            "current_aqi": aqi_data.get("us_aqi"),
            "current_pm25": aqi_data.get("pm2_5"),
            "current_pm10": aqi_data.get("pm10"),
            "humidity_now": forecast.get("current", {}).get("relative_humidity_2m"),
        }

    return render_template(
        "analytics.html", app_name=APP_NAME, city=city, place=place, chart_data=chart_data
    )

@app.route("/compare")
def compare():
    city_a = request.args.get("city_a", "New Delhi")
    city_b = request.args.get("city_b", "Mumbai")
    pkg_a = build_city_package(city_a)
    pkg_b = build_city_package(city_b)
    return render_template(
        "compare.html", app_name=APP_NAME, city_a=city_a, city_b=city_b, pkg_a=pkg_a, pkg_b=pkg_b
    )

@app.route("/alerts")
def alerts_page():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM weather_alerts ORDER BY created_at DESC LIMIT 50"
    ).fetchall()
    return render_template("alerts.html", app_name=APP_NAME, alerts=rows)

@app.route("/api/geolocate")
def api_geolocate():
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    if lat is None or lon is None:
        return jsonify({"error": "lat/lon required"}), 400
    try:
        forecast = fetch_forecast(lat, lon)
    except requests.RequestException:
        return jsonify({"error": "weather service unavailable"}), 502
    # reverse geocode isn't available on the free geocoding endpoint, so we
    # label the point using coordinates; the detail page re-resolves by name.
    return jsonify({"lat": lat, "lon": lon, "current": forecast.get("current", {})})


@app.route("/api/search-suggestions")
def api_search_suggestions():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    matches = geocode_city(q, count=6)
    return jsonify([
        {
            "name": m["name"],
            "country": m.get("country", ""),
            "admin1": m.get("admin1", ""),
            "lat": m["latitude"],
            "lon": m["longitude"],
        }
        for m in matches
    ])


@app.route("/api/favorites", methods=["POST", "DELETE"])
def api_favorites():
    uid = current_user_uuid()
    db = get_db()
    payload = request.get_json(force=True, silent=True) or {}
    city = payload.get("city")
    country = payload.get("country", "")
    lat = payload.get("lat")
    lon = payload.get("lon")

    if request.method == "POST":
        try:
            db.execute(
                "INSERT INTO favorite_cities (user_uuid, city, country, lat, lon, added_at) VALUES (?,?,?,?,?,?)",
                (uid, city, country, lat, lon, datetime.utcnow().isoformat()),
            )
            db.commit()
        except sqlite3.IntegrityError:
            pass
        return jsonify({"status": "added"})
    else:
        db.execute("DELETE FROM favorite_cities WHERE user_uuid=? AND city=?", (uid, city))
        db.commit()
        return jsonify({"status": "removed"})


@app.route("/api/theme", methods=["POST"])
def api_theme():
    uid = current_user_uuid()
    theme = (request.get_json(force=True, silent=True) or {}).get("theme", "dark")
    db = get_db()
    db.execute("UPDATE users SET theme=? WHERE uuid=?", (theme, uid))
    db.commit()
    return jsonify({"status": "ok", "theme": theme})


@app.route("/report/<city>.pdf")
def report_pdf(city):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.pdfgen import canvas

    package = build_city_package(city)
    if package is None:
        return "City not found", 404

    place = package["place"]
    current = package["current"]
    daily = package["daily"]
    ai = package["ai"]

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    margin = 20 * mm

    c.setFillColor(colors.HexColor("#0B1120"))
    c.rect(0, height - 40 * mm, width, 40 * mm, fill=True, stroke=False)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 22)
    c.drawString(margin, height - 20 * mm, f"WeatherSphere AI — {place['name']}")
    c.setFont("Helvetica", 11)
    c.drawString(margin, height - 28 * mm, f"{place.get('country','')}  •  Generated {datetime.utcnow().strftime('%d %b %Y, %H:%M UTC')}")

    y = height - 55 * mm
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(margin, y, "Current Conditions")
    y -= 8 * mm
    c.setFont("Helvetica", 11)
    lines = [
        f"Condition: {package['condition_label']}",
        f"Temperature: {current.get('temperature_2m')}°C  (feels like {current.get('apparent_temperature')}°C)",
        f"Humidity: {current.get('relative_humidity_2m')}%",
        f"Wind Speed: {current.get('wind_speed_10m')} km/h",
        f"Pressure: {current.get('pressure_msl')} hPa",
        f"Air Quality Index: {package['aqi']} ({package['aqi_label']})",
    ]
    for line in lines:
        c.drawString(margin, y, line)
        y -= 6.5 * mm

    y -= 4 * mm
    c.setFont("Helvetica-Bold", 14)
    c.drawString(margin, y, "AI Weather Summary")
    y -= 8 * mm
    c.setFont("Helvetica", 10)
    for chunk in _wrap_text(ai["summary"], 95):
        c.drawString(margin, y, chunk)
        y -= 5.5 * mm

    y -= 4 * mm
    c.setFont("Helvetica-Bold", 14)
    c.drawString(margin, y, "7-Day Outlook")
    y -= 8 * mm
    c.setFont("Helvetica", 10)
    times = daily.get("time", [])
    tmax = daily.get("temperature_2m_max", [])
    tmin = daily.get("temperature_2m_min", [])
    for i, day in enumerate(times):
        if y < 30 * mm:
            c.showPage()
            y = height - margin
        c.drawString(margin, y, f"{day}:  {tmin[i] if i < len(tmin) else '-'}°C  /  {tmax[i] if i < len(tmax) else '-'}°C")
        y -= 6 * mm

    c.showPage()
    c.save()
    buf.seek(0)
    return send_file(
        buf, mimetype="application/pdf", as_attachment=True,
        download_name=f"{place['name']}_weather_report.pdf",
    )


def _wrap_text(text, width):
    words = text.split()
    lines, current = [], ""
    for w in words:
        if len(current) + len(w) + 1 > width:
            lines.append(current)
            current = w
        else:
            current = f"{current} {w}".strip()
    if current:
        lines.append(current)
    return lines


if __name__ == "__main__":
    import os
    os.makedirs("database", exist_ok=True)
    init_db()
    app.run(debug=True, host="0.0.0.0", port=5000)
