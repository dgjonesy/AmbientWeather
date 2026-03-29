# AmbientWeather WS-2902 Local Receiver

A Python application that receives weather data directly from an Ambient Weather WS-2902 station over your local network — no cloud API or API keys required.

Features:
- **Local-only** — receives data directly from the station via HTTP
- **Rich terminal dashboard** — live-updating display of all weather metrics
- **InfluxDB + Grafana** — optional time-series storage and pre-built dashboard for historical data
- **Derived calculations** — computes dew point, heat index, and wind chill locally
- **Debug logging** — with automatic PASSKEY redaction

## Requirements

- Python 3.10+
- Ambient Weather WS-2902 station on the same LAN
- Docker (optional, for InfluxDB + Grafana)

## Quick Start

```bash
# Clone and set up
git clone https://github.com/dgjonesy/AmbientWeather.git
cd AmbientWeather
python3 -m venv .venv
source .venv/bin/activate  # or .venv/bin/activate.fish for fish shell
pip install -r requirements.txt

# Run the receiver
python weather.py
```

## Station Configuration

1. Open the **awnet** app on your phone
2. Go to **Custom** server settings
3. Configure:
   - **Protocol:** Ambient Weather
   - **Host:** your server's IP address
   - **Port:** 8080
   - **Path:** /data
4. Enable and save

The station will send data every 60 seconds.

## Usage

```bash
# Basic — terminal dashboard only
python weather.py

# With debug logging
python weather.py --debug

# Custom host/port
python weather.py --host 0.0.0.0 --port 9090

# With InfluxDB storage
python weather.py --influxdb --influxdb-token weather-station-token
```

### CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `0.0.0.0` | Address to listen on |
| `--port` | `8080` | Port to listen on |
| `--debug` | off | Enable debug logging to file |
| `--log-file` | `weather_debug.log` | Debug log file path |
| `--influxdb` | off | Enable writing data to InfluxDB |
| `--influxdb-url` | `http://localhost:8086` | InfluxDB URL |
| `--influxdb-token` | | InfluxDB auth token |
| `--influxdb-org` | `weather` | InfluxDB organization |
| `--influxdb-bucket` | `weather` | InfluxDB bucket |

Options can also be set via environment variables: `LISTEN_HOST`, `LISTEN_PORT`, `LOG_FILE`, `INFLUXDB_URL`, `INFLUXDB_TOKEN`, `INFLUXDB_ORG`, `INFLUXDB_BUCKET`.

## InfluxDB + Grafana

For historical data storage and visualization:

```bash
# Start InfluxDB and Grafana
docker compose up -d

# Run the receiver with InfluxDB enabled
python weather.py --influxdb --influxdb-token weather-station-token
```

Grafana is available at [http://localhost:3000](http://localhost:3000) with no login required. A pre-built 9-panel dashboard is auto-provisioned with:

- Outdoor & indoor temperature
- Humidity
- Barometric pressure
- Wind speed & direction
- Rainfall
- Solar radiation & UV index
- Current conditions summary

## Project Structure

```
weather.py                     # Main application
docker-compose.yml             # InfluxDB 2 + Grafana services
requirements.txt               # Python dependencies
grafana/
  dashboards/
    weather.json               # Pre-built Grafana dashboard
  provisioning/
    datasources/influxdb.yml   # InfluxDB datasource config
    dashboards/dashboards.yml  # Dashboard auto-provisioning
```

## License

MIT
