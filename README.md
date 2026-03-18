# 🌾 Agroalimentare — Bandi e Avvisi Bot

Bot Telegram per il monitoraggio automatico di bandi, avvisi e opportunità di finanziamento nel settore **agroalimentare**, sviluppato per **Legacoop Sicilia**.

## Fonti monitorate (25 totali)

### 🇪🇺 Europeo (7 fonti)
- Funding & Tenders Portal (UE)
- REA – Research Executive Agency (Horizon Europe Cluster 6)
- Horizon Europe NCP Portal – Cluster 6
- EIT Food – Innovazione Agrifood
- Europa Innovazione
- EuropaFacile (ART-ER)
- Obiettivo Europa

### 🇮🇹 Nazionale (9 fonti)
- MASAF – Ministero Agricoltura (PNRR, Parco Agrisolare, Contratti di Filiera)
- ISMEA (Investe, Più Impresa, Generazione Terra)
- Invitalia (Resto al Sud, Smart&Start)
- SIMEST – Fondo 394 Internazionalizzazione
- GSE – Parco Agrisolare e Rinnovabili
- FASI.eu – Fondi e Agevolazioni
- Italian Food News
- MIMIT – Nuova Sabatini, Transizione 5.0
- INAIL – Bando ISI

### 🏴 Sicilia (9 fonti)
- Sviluppo Rurale Regione Sicilia (CSR Sicilia PSP 2023-2027)
- PSR Sicilia
- Assessorato Agricoltura Regione Siciliana
- EuroInfoSicilia (FESR, FSC, Interreg)
- Sicilia Agricoltura
- Sicilia Rurale
- Terra – Portale Politiche Agricole Sicilia
- Dipartimento Pesca Mediterranea Sicilia
- Rete Rurale – GAL Siciliani (LEADER)

## Comandi bot

| Comando | Descrizione |
|---------|-------------|
| `/start` | Messaggio di benvenuto |
| `/cerca` | Ricerca con filtri (livello, rilevanza, destinatari, stato, parola chiave) |
| `/ultimi` | Ultimi 10 bandi aggiunti |
| `/scadenze` | Bandi in scadenza nei prossimi 30 giorni |
| `/fonti` | Lista fonti monitorate con statistiche |
| `/help` | Guida completa |
| `/test_daily` | Esegue manualmente il controllo giornaliero |

## Setup locale

### 1. Requisiti
- Python 3.11+
- Token bot Telegram

### 2. Installazione

```bash
git clone https://github.com/legacoopsicilia/agroalimentare-bandi-bot
cd agroalimentare-bandi-bot
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configurazione

Crea il file `.env` nella root del progetto:

```env
TELEGRAM_BOT_TOKEN=il_tuo_token_qui
TELEGRAM_CHAT_ID=il_tuo_chat_id_qui
```

### 4. Avvio

```bash
# Polling continuo (sviluppo locale)
python run.py

# Controllo giornaliero una tantum
python run.py --once

# Report settimanale una tantum
python run.py --weekly-once
```

## Deploy con GitHub Actions

Il bot usa GitHub Actions per lo scheduling (non richiede server always-on).

### Configurazione secrets

Nel repository GitHub, vai su **Settings → Secrets → Actions** e aggiungi:
- `TELEGRAM_BOT_TOKEN` — token del bot
- `TELEGRAM_CHAT_ID` — ID della chat/gruppo

### Schedule automatico

| Job | Orario | Giorno |
|-----|--------|--------|
| Daily check | 08:00 Europe/Rome | Ogni giorno |
| Weekly report | 08:05 Europe/Rome | Lunedì |

La tolleranza per il time guard è **±10 minuti**.

Il database SQLite viene persistito tra i run tramite GitHub Actions cache.

### Trigger manuale

Per eseguire un controllo manuale:
1. Vai su **Actions → daily** (o **weekly**)
2. Clicca **Run workflow**

## Architettura

```
src/agrobandi_bot/
├── config.py       # Configurazione YAML + env vars
├── db.py           # Database SQLite (aiosqlite)
├── filtering.py    # Scoring keyword-based + tag destinatari
├── formatting.py   # Formattazione messaggi Telegram HTML
├── http_client.py  # HTTP async con rate limiting e retry
├── models.py       # Dataclasses (Item, Source, SearchFilters, ...)
├── runner.py       # Pipeline daily/weekly
├── sources.py      # Scraper (RSS + HTML) per le 25 fonti
└── telegram_app.py # Bot PTB: comandi, ConversationHandler, scheduler
```

## Test

```bash
pytest tests/ -v
```

## Filtri disponibili in /cerca

- **Livello**: 🇪🇺 Europeo / 🇮🇹 Nazionale / 🏴 Sicilia / Tutti
- **Rilevanza cooperativa**: ⭐⭐⭐ Alta / ⭐⭐ Media / Tutte
- **Destinatari**: Cooperative / PMI / Giovani e Donne / Pesca / Tutti
- **Stato**: Aperto / In scadenza / Atteso / Tutti
- **Parola chiave**: ricerca libera su titolo e descrizione

I risultati sono paginati (5 per pagina) con navigazione ◀ ▶.

---

📡 **Legacoop Sicilia** — Monitoraggio automatico bandi agroalimentari
