#!/usr/bin/env python3
"""
Ambient Weather WS-2902 Local Receiver

Runs a local HTTP server that receives weather data directly from your
WS-2902 station over your LAN. No cloud API or API keys needed.

Setup:
  1. Open the awnet app on your phone
  2. Go to "Custom" server settings
  3. Set: Protocol=Ambient Weather, Host=<this machine's IP>, Port=8080, Path=/data
  4. Enable and save

Run:
  python weather.py
  python weather.py --debug
  python weather.py --influxdb
  python weather.py --host 0.0.0.0 --port 9090
"""

import argparse
import logging
import math
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

log = logging.getLogger("weather")

_PASSKEY_RE = re.compile(r"(PASSKEY=)[A-Fa-f0-9]+", re.IGNORECASE)

def _redact(s: str) -> str:
    return _PASSKEY_RE.sub(r"\1[REDACTED]", s)

console = Console()

# Shared state: latest weather data from the station
_lock = threading.Lock()
_latest_data: dict | None = None
_influx_writer = None  # Set in main() if --influxdb is used


# ---------------------------------------------------------------------------
# InfluxDB integration
# ---------------------------------------------------------------------------

# Fields to write as InfluxDB float/int measurements
INFLUX_FLOAT_FIELDS = {
    "tempf", "tempinf", "dewPoint", "dewPointin", "feelsLike", "feelsLikein",
    "windspeedmph", "windgustmph", "maxdailygust", "windspdmph_avg2m",
    "windspdmph_avg10m", "baromrelin", "baromabsin", "solarradiation",
    "hourlyrainin", "dailyrainin", "weeklyrainin", "monthlyrainin",
    "yearlyrainin", "eventrainin", "totalrainin", "24hourrainin",
}
INFLUX_INT_FIELDS = {
    "winddir", "humidity", "humidityin", "uv", "battout", "battin",
}


def init_influx_writer(url: str, token: str, org: str, bucket: str):
    """Initialize the InfluxDB write client. Returns the (client, write_api) tuple."""
    from influxdb_client import InfluxDBClient
    from influxdb_client.client.write_api import SYNCHRONOUS
    client = InfluxDBClient(url=url, token=token, org=org)
    write_api = client.write_api(write_options=SYNCHRONOUS)
    return client, write_api, bucket, org


def write_to_influx(data: dict) -> None:
    """Write a weather data point to InfluxDB."""
    global _influx_writer
    if _influx_writer is None:
        return

    from influxdb_client import Point
    client, write_api, bucket, org = _influx_writer

    point = Point("weather")
    mac = data.get("MAC", "unknown")
    point.tag("station", mac)

    for key in INFLUX_FLOAT_FIELDS:
        val = data.get(key)
        if val is not None and isinstance(val, (int, float)):
            point.field(key, float(val))

    for key in INFLUX_INT_FIELDS:
        val = data.get(key)
        if val is not None and isinstance(val, (int, float)):
            point.field(key, int(val))

    try:
        write_api.write(bucket=bucket, org=org, record=point)
        log.debug("InfluxDB write OK")
    except Exception as exc:
        log.error("InfluxDB write failed: %s", exc)


# ---------------------------------------------------------------------------
# Derived weather calculations (normally done server-side)
# ---------------------------------------------------------------------------

def calc_dew_point(temp_f: float, humidity: float) -> float:
    """Magnus formula dew point. Returns °F."""
    temp_c = (temp_f - 32) * 5 / 9
    a, b = 17.27, 237.7
    alpha = (a * temp_c) / (b + temp_c) + math.log(humidity / 100)
    dew_c = (b * alpha) / (a - alpha)
    return dew_c * 9 / 5 + 32


def calc_heat_index(temp_f: float, humidity: float) -> float:
    """Rothfusz regression heat index. Returns °F."""
    t, h = temp_f, humidity
    hi = 0.5 * (t + 61.0 + (t - 68.0) * 1.2 + h * 0.094)
    if hi >= 80:
        hi = (
            -42.379
            + 2.04901523 * t
            + 10.14333127 * h
            - 0.22475541 * t * h
            - 0.00683783 * t * t
            - 0.05481717 * h * h
            + 0.00122874 * t * t * h
            + 0.00085282 * t * h * h
            - 0.00000199 * t * t * h * h
        )
        if h < 13 and 80 <= t <= 112:
            hi -= ((13 - h) / 4) * math.sqrt((17 - abs(t - 95)) / 17)
        elif h > 85 and 80 <= t <= 87:
            hi += ((h - 85) / 10) * ((87 - t) / 5)
    return hi


def calc_wind_chill(temp_f: float, wind_mph: float) -> float:
    """NWS wind chill formula. Returns °F."""
    if wind_mph <= 3:
        return temp_f
    return (
        35.74
        + 0.6215 * temp_f
        - 35.75 * (wind_mph ** 0.16)
        + 0.4275 * temp_f * (wind_mph ** 0.16)
    )


def calc_feels_like(temp_f: float, humidity: float, wind_mph: float) -> float:
    if temp_f < 50:
        return calc_wind_chill(temp_f, wind_mph)
    if temp_f > 68:
        return calc_heat_index(temp_f, humidity)
    return temp_f


def enrich_data(d: dict) -> dict:
    """Add computed fields that Ambient's cloud normally provides."""
    tempf = d.get("tempf")
    humidity = d.get("humidity")
    wind = d.get("windspeedmph", 0)

    if tempf is not None and humidity is not None:
        d.setdefault("dewPoint", round(calc_dew_point(tempf, humidity), 1))
        d.setdefault("feelsLike", round(calc_feels_like(tempf, humidity, wind), 1))

    tempinf = d.get("tempinf")
    humidityin = d.get("humidityin")
    if tempinf is not None and humidityin is not None:
        d.setdefault("dewPointin", round(calc_dew_point(tempinf, humidityin), 1))
        d.setdefault("feelsLikein", round(calc_feels_like(tempinf, humidityin, 0), 1))

    return d


# ---------------------------------------------------------------------------
# Parse incoming data from the station
# ---------------------------------------------------------------------------

FLOAT_FIELDS = {
    "windspeedmph", "windgustmph", "maxdailygust", "windspdmph_avg2m",
    "windspdmph_avg10m", "tempf", "tempinf", "hourlyrainin", "dailyrainin",
    "weeklyrainin", "monthlyrainin", "yearlyrainin", "eventrainin",
    "totalrainin", "baromrelin", "baromabsin", "solarradiation",
    "dewPoint", "feelsLike", "24hourrainin",
}

INT_FIELDS = {
    "winddir", "windgustdir", "winddir_avg2m", "winddir_avg10m",
    "humidity", "humidityin", "uv", "battout", "battin",
}


def parse_station_params(params: dict[str, list[str]]) -> dict:
    """Convert query-string params to typed dict."""
    data = {}
    for key, values in params.items():
        val = values[0]
        if key in FLOAT_FIELDS:
            try:
                data[key] = float(val)
            except ValueError:
                data[key] = val
        elif key in INT_FIELDS:
            try:
                data[key] = int(float(val))
            except ValueError:
                data[key] = val
        else:
            data[key] = val
    return data


# ---------------------------------------------------------------------------
# HTTP handler — receives GET from the WS-2902
# ---------------------------------------------------------------------------

class WeatherHandler(BaseHTTPRequestHandler):
    def _handle_data(self, params: dict[str, list[str]]):
        global _latest_data
        data = parse_station_params(params)
        data.pop("PASSKEY", None)
        data["_received"] = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
        enrich_data(data)
        log.info("DATA received — MAC=%s tempf=%s humidity=%s",
                 data.get("MAC", "?"), data.get("tempf", "?"), data.get("humidity", "?"))
        log.debug("Full data: %s", data)

        with _lock:
            _latest_data = data

        write_to_influx(data)

        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK\n")

    def do_GET(self):
        log.info("GET %s from %s", _redact(self.path), self.client_address)
        # The WS-2902 sends /data&key=val&... instead of /data?key=val&...
        # Normalize by replacing the first & with ? if there's no query string
        path = self.path
        if "?" not in path and "&" in path:
            path = path.replace("&", "?", 1)
        parsed = urlparse(path)
        params = parse_qs(parsed.query)

        if not params:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Ambient Weather receiver running.\n")
            return

        self._handle_data(params)

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8", errors="replace")
        log.info("POST %s from %s body_len=%d", _redact(self.path), self.client_address, len(body))
        log.debug("POST body: %s", _redact(body))
        params = parse_qs(body)
        if not params:
            params = parse_qs(urlparse(self.path).query)
        self._handle_data(params)

    def log_message(self, format, *args):
        log.debug("HTTP: " + _redact(format % args))


# ---------------------------------------------------------------------------
# Rich display panels
# ---------------------------------------------------------------------------

def wind_direction_label(degrees: float | None) -> str:
    if degrees is None:
        return "N/A"
    dirs = [
        "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
        "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
    ]
    idx = round(degrees / 22.5) % 16
    return dirs[idx]


def fmt(value, unit: str = "", decimals: int = 1) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.{decimals}f}{unit}"
    return f"{value}{unit}"


def pressure_trend_symbol(current: float | None) -> str:
    if current is None:
        return ""
    if current > 30.2:
        return " [bold blue]H[/]"
    if current < 29.8:
        return " [bold red]L[/]"
    return ""


def build_outdoor_panel(d: dict) -> Panel:
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("label", style="bold cyan", no_wrap=True)
    table.add_column("value", style="white", no_wrap=True)

    table.add_row("Temperature", fmt(d.get("tempf"), "°F"))
    table.add_row("Feels Like", fmt(d.get("feelsLike"), "°F"))
    table.add_row("Dew Point", fmt(d.get("dewPoint"), "°F"))
    table.add_row("Humidity", fmt(d.get("humidity"), "%", 0))

    return Panel(table, title="[bold green]Outdoor[/]", border_style="green")


def build_wind_panel(d: dict) -> Panel:
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("label", style="bold cyan", no_wrap=True)
    table.add_column("value", style="white", no_wrap=True)

    wind_dir = d.get("winddir")
    table.add_row(
        "Wind",
        f"{fmt(d.get('windspeedmph'), ' mph')} {wind_direction_label(wind_dir)} ({fmt(wind_dir, '°', 0)})",
    )
    table.add_row("Wind Gust", fmt(d.get("windgustmph"), " mph"))
    table.add_row("Max Daily Gust", fmt(d.get("maxdailygust"), " mph"))
    table.add_row("Avg Wind (2 min)", fmt(d.get("windspdmph_avg2m"), " mph"))
    table.add_row("Avg Wind (10 min)", fmt(d.get("windspdmph_avg10m"), " mph"))

    return Panel(table, title="[bold yellow]Wind[/]", border_style="yellow")


def build_rain_panel(d: dict) -> Panel:
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("label", style="bold cyan", no_wrap=True)
    table.add_column("value", style="white", no_wrap=True)

    table.add_row("Hourly Rate", fmt(d.get("hourlyrainin"), " in/hr", 2))
    table.add_row("Event Rain", fmt(d.get("eventrainin"), " in", 2))
    table.add_row("Daily Rain", fmt(d.get("dailyrainin"), " in", 2))
    table.add_row("Weekly Rain", fmt(d.get("weeklyrainin"), " in", 2))
    table.add_row("Monthly Rain", fmt(d.get("monthlyrainin"), " in", 2))
    table.add_row("Yearly Rain", fmt(d.get("yearlyrainin"), " in", 2))

    return Panel(table, title="[bold blue]Rain[/]", border_style="blue")


def build_pressure_panel(d: dict) -> Panel:
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("label", style="bold cyan", no_wrap=True)
    table.add_column("value", style="white", no_wrap=True)

    rel = d.get("baromrelin")
    abso = d.get("baromabsin")
    table.add_row("Relative Pressure", fmt(rel, " inHg", 2) + pressure_trend_symbol(rel))
    table.add_row("Absolute Pressure", fmt(abso, " inHg", 2))

    return Panel(table, title="[bold magenta]Barometer[/]", border_style="magenta")


def build_solar_panel(d: dict) -> Panel:
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("label", style="bold cyan", no_wrap=True)
    table.add_column("value", style="white", no_wrap=True)

    table.add_row("Solar Radiation", fmt(d.get("solarradiation"), " W/m²"))
    table.add_row("UV Index", fmt(d.get("uv"), "", 0))

    return Panel(table, title="[bold yellow]Solar / UV[/]", border_style="yellow")


def build_indoor_panel(d: dict) -> Panel:
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("label", style="bold cyan", no_wrap=True)
    table.add_column("value", style="white", no_wrap=True)

    table.add_row("Temperature", fmt(d.get("tempinf"), "°F"))
    table.add_row("Feels Like", fmt(d.get("feelsLikein"), "°F"))
    table.add_row("Humidity", fmt(d.get("humidityin"), "%", 0))
    table.add_row("Dew Point", fmt(d.get("dewPointin"), "°F"))

    return Panel(table, title="[bold red]Indoor[/]", border_style="red")


def build_battery_panel(d: dict) -> Panel:
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("label", style="bold cyan", no_wrap=True)
    table.add_column("value", style="white", no_wrap=True)

    for key, label in [("battout", "Outdoor Sensor"), ("battin", "Indoor Sensor")]:
        val = d.get(key)
        if val is not None:
            status = "[green]OK[/]" if val == 1 else "[red]LOW[/]"
            table.add_row(label, status)

    return Panel(table, title="[bold white]Batteries[/]", border_style="white")


def build_display(data: dict | None) -> Layout:
    d = data or {}

    mac = d.get("MAC", "WS-2902")
    received = d.get("_received", "waiting for data...")
    dateutc = d.get("dateutc", "")
    if dateutc:
        try:
            dt = datetime.strptime(dateutc, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            dateutc = dt.astimezone().strftime("%Y-%m-%d %I:%M:%S %p")
        except ValueError:
            pass

    if data is None:
        status = "[bold yellow]Waiting for first report from station...[/]"
    else:
        status = (
            f"Station: [bold white]{mac}[/]  |  "
            f"Station time: [cyan]{dateutc}[/]  |  "
            f"Received: [cyan]{received}[/]"
        )

    header_text = Text.from_markup(
        f"{status}  |  Press [bold]Ctrl+C[/] to quit"
    )

    layout = Layout()
    layout.split_column(
        Layout(Panel(header_text, style="bold"), name="header", size=3),
        Layout(name="top", size=8),
        Layout(name="middle", size=8),
        Layout(name="bottom", size=10),
    )

    layout["top"].split_row(
        Layout(build_outdoor_panel(d)),
        Layout(build_wind_panel(d)),
    )
    layout["middle"].split_row(
        Layout(build_pressure_panel(d)),
        Layout(build_indoor_panel(d)),
    )
    layout["bottom"].split_row(
        Layout(build_rain_panel(d)),
        Layout(build_solar_panel(d)),
        Layout(build_battery_panel(d)),
    )

    return layout


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ambient Weather WS-2902 Local Receiver — "
                    "receives weather data directly from your station over LAN.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "WS-2902 setup (via awnet app → Custom server):\n"
            "  Protocol:  Ambient Weather\n"
            "  Host:      <this machine's LAN IP>\n"
            "  Port:      8080 (or value of --port)\n"
            "  Path:      /data\n"
            "  Interval:  60 seconds\n"
            "\n"
            "Environment variables (override defaults):\n"
            "  LISTEN_HOST       listen address       (default: 0.0.0.0)\n"
            "  LISTEN_PORT       listen port          (default: 8080)\n"
            "  LOG_FILE          debug log path       (default: weather_debug.log)\n"
            "  INFLUXDB_URL      InfluxDB URL         (default: http://localhost:8086)\n"
            "  INFLUXDB_TOKEN    InfluxDB auth token\n"
            "  INFLUXDB_ORG      InfluxDB org          (default: weather)\n"
            "  INFLUXDB_BUCKET   InfluxDB bucket       (default: weather)\n"
        ),
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="enable debug logging to file (default: weather_debug.log)",
    )
    parser.add_argument(
        "--host", default=os.environ.get("LISTEN_HOST", "0.0.0.0"),
        help="address to listen on (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("LISTEN_PORT", "8080")),
        help="port to listen on (default: 8080)",
    )
    parser.add_argument(
        "--log-file", default=os.environ.get("LOG_FILE", "weather_debug.log"),
        help="debug log file path (default: weather_debug.log)",
    )
    parser.add_argument(
        "--influxdb", action="store_true",
        help="enable writing data to InfluxDB",
    )
    parser.add_argument(
        "--influxdb-url",
        default=os.environ.get("INFLUXDB_URL", "http://localhost:8086"),
        help="InfluxDB URL (default: http://localhost:8086)",
    )
    parser.add_argument(
        "--influxdb-token",
        default=os.environ.get("INFLUXDB_TOKEN", ""),
        help="InfluxDB auth token (or set INFLUXDB_TOKEN env var)",
    )
    parser.add_argument(
        "--influxdb-org",
        default=os.environ.get("INFLUXDB_ORG", "weather"),
        help="InfluxDB organization (default: weather)",
    )
    parser.add_argument(
        "--influxdb-bucket",
        default=os.environ.get("INFLUXDB_BUCKET", "weather"),
        help="InfluxDB bucket (default: weather)",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    if args.debug:
        logging.basicConfig(
            filename=args.log_file,
            level=logging.DEBUG,
            format="%(asctime)s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    else:
        logging.disable(logging.CRITICAL)

    log.info("=== Weather receiver starting on %s:%d ===", args.host, args.port)

    if args.influxdb:
        global _influx_writer
        if not args.influxdb_token:
            console.print(
                "[bold red]Error:[/] --influxdb requires a token. "
                "Set --influxdb-token or INFLUXDB_TOKEN env var."
            )
            sys.exit(1)
        _influx_writer = init_influx_writer(
            args.influxdb_url, args.influxdb_token,
            args.influxdb_org, args.influxdb_bucket,
        )
        console.print(
            f"[bold green]InfluxDB:[/] writing to {args.influxdb_url} "
            f"org=[bold]{args.influxdb_org}[/] bucket=[bold]{args.influxdb_bucket}[/]"
        )

    server = HTTPServer((args.host, args.port), WeatherHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    console.print(f"[bold green]Listening on {args.host}:{args.port}[/]")
    if args.debug:
        console.print(f"[dim]Debug log:[/] [bold]{args.log_file}[/]")
    console.print(
        "[dim]Configure your WS-2902 custom server:[/]\n"
        f"  Protocol: [bold]Ambient Weather[/]  |  Host: [bold]<this machine's IP>[/]  |  "
        f"Port: [bold]{args.port}[/]  |  Path: [bold]/data[/]\n"
    )

    time.sleep(1)

    with Live(build_display(None), console=console, refresh_per_second=1, screen=True) as live:
        while True:
            try:
                time.sleep(1)
                with _lock:
                    data = _latest_data
                live.update(build_display(data))
            except KeyboardInterrupt:
                break

    server.shutdown()


if __name__ == "__main__":
    main()
