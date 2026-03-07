# Biedawkobot

Scrapes promotional leaflets from Biedronka and Lidl, extracts sale items using a local vision model (Ollama), and exposes them via a REST API.

## Architecture

```
┌─────────────────────────────────────────────────┐
│                   Host machine                  │
│                                                 │
│  scrape_biedronka.py ──┐                        │
│  scrape_lidl.py ───────┼──► leaflets/           │
│                        │       └── {provider}/  │
│  parse_sales.py ───────┘           └── {uuid}/  │
│      │  ▲                               └── page_NNN.jpg
│      │  └── Ollama (localhost:11434)    │        │
│      ▼                                  │        │
│  MongoDB (localhost:27017) ◄────────────┘        │
│      ▲                                           │
└──────┼───────────────────────────────────────────┘
       │  (Docker network)
  ┌────┴─────┐
  │  api     │  ► http://localhost:8000
  └──────────┘
```

The scrapers and parser run on the **host**. MongoDB and the API run in **Docker**.

## Prerequisites

- Python 3.11+
- Docker (or Podman + podman-compose)
- [Ollama](https://ollama.com/) with a vision model pulled (default: `qwen3-vl`)

```sh
ollama pull qwen3-vl
```

## Setup

### 1. Configure environment

```sh
cp .env.example .env
```

Edit `.env` as needed. The defaults work out of the box for local development.

### 2. Start MongoDB and the API

```sh
docker compose up -d
# or: podman-compose up -d
```

The API will be available at **http://localhost:8000**.

### 3. Install Python dependencies

```sh
pip install -r requirements.txt
playwright install chromium
```

## Usage

### Scraping leaflets

Download all current leaflet pages from Biedronka and Lidl:

```sh
python scrape_biedronka.py        # scrape all Biedronka leaflets
python scrape_lidl.py             # scrape all Lidl leaflets
```

You can also target a single leaflet:

```sh
# Biedronka: pass the leaflet page URL
python scrape_biedronka.py https://www.biedronka.pl/pl/gazetki,gazetka-...

# Lidl: pass the flyer identifier (URL slug from the gazetki page)
python scrape_lidl.py oferta-wazna-od-2-03-do-4-03-gazetka-pon-kw10
```

Downloaded images are saved to `leaflets/{provider}/{uuid}/page_NNN[_I].jpg` and are automatically deleted after parsing. Both scrapers resume gracefully if interrupted.

### Parsing sales

Process downloaded leaflet images with the vision model and insert sale items into MongoDB:

```sh
python parse_sales.py             # process all pending leaflets
python parse_sales.py --debug     # also write approved.txt / failed.txt for inspection
```

You can also target a specific image or folder:

```sh
python parse_sales.py path/to/image.jpg
python parse_sales.py path/to/leaflet/folder/
```

**Environment variables for the parser:**

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama API endpoint |
| `OLLAMA_MODEL` | `qwen3-vl` | Vision model to use |

### Optional: run Ollama in Docker

Uncomment the `ollama` service in `docker-compose.yml` if you prefer to run it containerised rather than on the host.

## API

Base URL: `http://localhost:8000`

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Health check — returns `{"status": "ok"}` |
| `GET` | `/sales` | Query sale items |

### `GET /sales` query parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `provider` | string | — | Filter by shop: `biedronka` or `lidl` |
| `category` | string | — | Partial, case-insensitive category match |
| `active` | bool | `false` | Only return promotions valid today |

**Examples:**

```sh
curl "http://localhost:8000/sales"
curl "http://localhost:8000/sales?provider=biedronka"
curl "http://localhost:8000/sales?category=nabia%C5%82"
curl "http://localhost:8000/sales?provider=lidl&active=true"
```
