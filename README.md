# FreightAI — AI-Powered Freight Negotiation Platform

> Autonomous AI agents that negotiate freight rates with multiple transporters in parallel — so you don't have to.

**Live Demo →** [v0-negotiation-agent-ui.vercel.app](https://v0-negotiation-agent-ui.vercel.app/)

---

## What is FreightAI?

FreightAI is a B2B logistics SaaS platform that automates freight rate procurement for manufacturers and shippers. Instead of manually calling transporters and negotiating rates, clients submit a shipment request and an AI agent handles the entire negotiation — running parallel conversations with multiple LSPs (Logistics Service Providers), countering quotes, and recommending the best deal.

**The core loop:**

```
Client posts RFQ → AI agent negotiates with N transporters in parallel
→ Scores each LSP → Recommends best deal → Client accepts / pushes another round
```

---

## Key Features

- **Parallel AI Negotiation** — the agent simultaneously negotiates with multiple transporters, not sequentially
- **LangGraph-powered agent** — stateful multi-step reasoning using Google Gemini as the LLM backbone
- **Live WebSocket updates** — negotiation state pushed to the frontend in real time
- **Transporter Portal** — LSPs can log in, view open RFQs, and submit bids directly
- **Scoring & Recommendation** — each LSP is scored on Quote, Profile, and Reliability; the agent explains its recommendation in plain language
- **Simulator** — a transporter simulator allows testing the negotiation loop without real LSPs

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend Framework | FastAPI |
| ASGI Server | Uvicorn |
| AI Agent Framework | LangGraph |
| LLM | Google Gemini (`langchain-google-genai`) |
| Real-time | WebSockets |
| Data Validation | Pydantic v2 |
| Data Store | JSON flat files (`/data` folder) |
| Frontend | Vercel (separate repo) |

---

## Project Structure

```
LN-Agentic-Hackathon/
├── main.py                  # FastAPI app entry point, route registration
├── websocket_manager.py     # WebSocket connection & broadcast manager
├── requirements.txt
├── .env                     # Environment variables (not committed)
│
├── agent/                   # LangGraph agent definition & negotiation graph
├── llm/                     # Gemini LLM client setup & prompt templates
├── logic/                   # Core negotiation logic, scoring, recommendation
├── simulators/              # Transporter simulator for testing negotiations
├── routers/                 # FastAPI route handlers (RFQ, transporter, negotiation)
├── models/                  # Pydantic models / schemas
├── db/                      # Data access helpers (reads/writes JSON files)
└── data/                    # Flat-file data store (JSON objects and arrays)
```

---

## Getting Started

### Prerequisites

- Python 3.10+
- A [Google AI Studio](https://aistudio.google.com/) API key (Gemini)

### 1. Clone the repo

```bash
git clone https://github.com/dodomyg/LN-Agentic-Hackathon.git
cd LN-Agentic-Hackathon
```

### 2. Create and activate a virtual environment

```bash
python -m venv venv
source venv/bin/activate        # macOS / Linux
venv\Scripts\activate           # Windows
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Set up environment variables

Create a `.env` file in the root directory:

```env
GEMINI_API_KEY=your_google_gemini_api_key_here
```

### 5. Run the server

```bash
uvicorn main:app --reload
```

The API will be available at `http://localhost:8000`.  
Interactive docs at `http://localhost:8000/docs`.

---

## How the Negotiation Works

1. **Client submits an RFQ** with origin, destination, truck type, capacity, budget, and target transporters.
2. **The LangGraph agent initialises** a negotiation session and notifies all selected LSPs.
3. **Each LSP submits an initial quote** (either via the Transporter Portal or the simulator).
4. **The agent evaluates each quote** against the budget and benchmark, then decides to accept, counter, or drop each LSP independently.
5. **Counter-offers are sent** and the cycle repeats for N rounds.
6. **Once stopping conditions are met**, the agent scores all LSPs and surfaces a recommendation with reasoning.
7. **The client accepts, rejects, or pushes one more round** from the Recommendation Panel.
8. **All updates are streamed live** to the frontend via WebSocket throughout.

---

## API Overview

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/rfq` | Create a new shipment RFQ |
| `GET` | `/rfq` | List all RFQs |
| `GET` | `/rfq/{id}` | Get RFQ details and negotiation state |
| `POST` | `/quote` | Transporter submits a bid |
| `GET` | `/transporters` | List all registered LSPs |
| `WS` | `/ws/{rfq_id}` | WebSocket for live negotiation updates |

> Full interactive API docs available at `/docs` when running locally.

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | Yes | Google Gemini API key from AI Studio |

---

## Frontend

The frontend is a separate project deployed on Vercel.

**Live:** [v0-negotiation-agent-ui.vercel.app](https://v0-negotiation-agent-ui.vercel.app/)

It connects to this backend via REST and WebSocket. To point it at a local backend, update the API base URL in the frontend environment config.

---

## Notes

- Data is currently persisted as JSON flat files in the `/data` directory. There is no database dependency — no setup or migrations required.
- The `simulators/` module lets you run a full negotiation loop locally without needing real transporter logins, useful for development and testing.

---

## Built at

**LN Agentic Hackathon** — exploring autonomous multi-agent systems for logistics procurement.
