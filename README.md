# AmarBhumi — আমার ভূমি

**Bangladesh’s modern land intelligence portal**

[![Live Demo](https://img.shields.io/badge/Live%20Demo-amarbhumi--1.onrender.com-blue?style=for-the-badge&logo=render)](https://amarbhumi-1.onrender.com/)
[![GitHub](https://img.shields.io/badge/GitHub-mdsuzon--hub%2FAmarBhumi-181717?style=for-the-badge&logo=github)](https://github.com/mdsuzon-hub/AmarBhumi)
[![License: Non-Commercial](https://img.shields.io/badge/License-Non--Commercial-orange.svg?style=for-the-badge)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.x-blue?style=for-the-badge&logo=python)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-Framework-black?style=for-the-badge&logo=flask)](https://flask.palletsprojects.com/)
[![Leaflet](https://img.shields.io/badge/Leaflet-Map-green?style=for-the-badge&logo=leaflet)](https://leafletjs.com/)

<p align="left">
  <a href="https://github.com/mdsuzon-hub/AmarBhumi">
    <img src="https://img.shields.io/github/stars/mdsuzon-hub/AmarBhumi?style=for-the-badge&logo=github&label=%E2%AD%90%20Star%20this%20repo&color=yellow&labelColor=333" alt="Star this repo">
  </a>
</p>

![Visitors](https://visitor-badge.laobi.icu/badge?page_id=mdsuzon-hub.AmarBhumi)
![GitHub stars](https://img.shields.io/github/stars/mdsuzon-hub/AmarBhumi?style=social)
![GitHub forks](https://img.shields.io/github/forks/mdsuzon-hub/AmarBhumi?style=social)

Find any plot on the map. Verify ownership before you buy. Track mutation cases in real time. Detect fraud before it becomes a dispute.

---

### Live Demo
**https://amarbhumi-1.onrender.com/**  
*(Demo environment — switch between Demo Data and Government API from the header)*

## 📸 Live Demo Screenshots

### Interactive Land Map
| Map View 1 | Map View 2 | Map View 3 |
|:----------:|:----------:|:----------:|
| ![Map 1](docs/screenshots/map_1.png) | ![Map 2](docs/screenshots/map_2.png) | ![Map 3](docs/screenshots/map_3.png) |

### Reports & Analysis
| Report 1 | Report 2 |
|:--------:|:--------:|
| ![Report 1](docs/screenshots/report_1.png) | ![Report 2](docs/screenshots/report_2.png) |

### PDF Reports (Portrait)
| PDF Report 1 | PDF Report 2 |
|:------------:|:------------:|
| ![PDF Report 1](docs/screenshots/report_pdf_1.png) | ![PDF Report 2](docs/screenshots/report_pdf_2.png) |

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