# AmarBhumi — আমার ভূমি

**Bangladesh’s modern land intelligence portal**

Find any plot on the map. Verify ownership before you buy. Track mutation cases in real time. Detect fraud before it becomes a dispute.

---

### Live Demo
**https://amarbhumi-1.onrender.com/**  
*(Demo environment — switch between Demo Data and Government API from the header)*

---

## Why AmarBhumi?

Land transactions in Bangladesh are still slowed by paper records, opaque ownership history, and the constant risk of forged deeds or double sales. AmarBhumi brings clarity, speed and trust to the process — for citizens, lawyers, and land officers alike.

Built as a complete MVP with **Flask + Leaflet**, it delivers a beautiful bilingual (বাংলা / English) experience that works on desktop and mobile.

## Two Powerful Data Modes

| Mode | Description | Best for |
|------|-------------|----------|
| **Demo Data** | Ready-to-use sample datasets (Gazipur, Dhaka, Cumilla). Instant exploration with realistic ownership, mutation and fraud scenarios. | Demos, training, offline testing, presentations |
| **Government API** | Live connection to the official Bangladesh land map service (`map.land.gov.bd`). Load any Division → District → Thana → Mouza, save the area, and work with real plot geometry and land-use data. | Real verification, pilot projects, production-ready workflows |

Switch modes anytime from the header. Each mode keeps its own data, cases and admin views completely isolated.

## Core Features

- **Interactive Land Map** — Satellite imagery, plot boundaries, land-use layers, search by plot/mouza/owner, place search, and one-click plot details.
- **Pre-Purchase Verification** — Instant risk score, ownership history, pending cases, khas/vested status, acquisition flags and clear advice.
- **e-Mutation Tracking** — Follow any case from deed registration to khatian with stage-by-stage progress and automated checks.
- **Fraud Detection Engine** — Flags forged deeds, double sales, rapid transfers and record mismatches with evidence and confidence scores.
- **Admin Tools** — Overview dashboard, fraud alert management, mutation workflow board, and data manager (upload datasets or load new API areas).
- **Fully Bilingual** — Seamless বাংলা / English toggle with proper Bengali typography and number formatting.

## Quick Start

```bash
pip install flask
python land_portal.py
```

Open **http://127.0.0.1:5000**

Optional flags:
```bash
python land_portal.py --port 8000 --no-browser
python land_portal.py --dev
```

### Environment Variables
| Variable | Purpose |
|----------|---------|
| `BD_MAP_URL` | Override government map service URL (default: `https://map.land.gov.bd`) |
| `BD_ADMIN_KEY` | Optional secret key required for all `/api/admin/*` endpoints |

## Project Structure

```
├── land_portal.py          # Single-file application (backend + frontend)
├── data/                   # Demo datasets + saved API areas
│   ├── *_land.json
│   ├── *_plot.json
│   └── api/                # Areas loaded from government API
└── README.md
```

## Technology

- **Backend**: Python 3 + Flask
- **Frontend**: Vanilla JS, Leaflet, modern CSS (light/dark theme)
- **Data**: GeoJSON + WKT, deterministic dummy ownership & fraud records for demo mode
- **API Proxy**: Secure session handling for the official land.gov.bd map service

## Who Is It For?

- Citizens checking a plot before purchase  
- Lawyers and deed writers verifying title  
- Union Land Offices and AC (Land) officers  
- Researchers and NGOs working on land transparency  
- Government pilots looking for a modern digital land experience  

---

**AmarBhumi** turns complex land records into clear, actionable insight — so every transaction can start with confidence.

*MVP built for demonstration and rapid prototyping. Ownership, case and fraud data in Demo mode are fictional.*