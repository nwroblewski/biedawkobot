# Biedawkobot

Scrapes promotional leaflets from Biedronka and Lidl, parses sale items using OCR, and exposes them via a REST API.

## Running the project

### 1. Configure environment

```sh
cp .env.example .env
```

Edit `.env` if needed — in particular set `MONGO_DATA_PATH` to the host path where MongoDB data should be persisted.

### 2. Start MongoDB and the API

```sh
python -m podman_compose up -d
```

> If `podman-compose` is on your PATH you can use that directly instead.

The API will be available at **http://localhost:8000**.

### 3. Run the scrapers

The scrapers run on the host (outside Docker) and store downloaded leaflet images locally.

```sh
pip install -r requirements.txt
playwright install chromium

python scrape_biedronka.py   # scrape Biedronka leaflets
python scrape_lidl.py        # scrape Lidl leaflets
```

### 4. Parse sales

Processes the downloaded images with OCR and inserts sale items into MongoDB.

```sh
python parse_sales.py
```

## API

| Endpoint | Description |
|---|---|
| `GET /health` | Health check |
| `GET /sales` | Query sale items |

### `GET /sales` query parameters

| Parameter | Type | Description |
|---|---|---|
| `provider` | string | Filter by shop: `biedronka` or `lidl` |
| `category` | string | Partial case-insensitive category match |
| `active` | bool | Only return promotions valid today (default: `false`) |
