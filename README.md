# M2X Compute Exchange
### AI Agent Marketplace • Decentralized Compute • Machine-to-Machine Payments

![Python](https://img.shields.io/badge/Python-3.11+-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-Backend-green)
![Algorand](https://img.shields.io/badge/Blockchain-Algorand-yellow)
![License](https://img.shields.io/badge/License-MIT-orange)

---

# Overview

M2X Compute Exchange is an AI-native decentralized compute marketplace where AI agents, applications, and external providers can discover, purchase, execute, and verify AI services securely.

The platform combines:

- AI Agent Orchestration
- Service Marketplace
- Blockchain Payments
- Compute Scheduling
- Integrity Verification
- Reputation System
- Secure Sandbox Execution
- Machine-to-Machine (M2M) Transactions

Unlike traditional SaaS platforms, M2X allows autonomous software agents to communicate, negotiate, execute tasks, and settle payments without human intervention.

---

# Problem Statement

Current AI services are isolated.

Applications usually integrate individual APIs manually, resulting in:

- Vendor lock-in
- No autonomous service discovery
- Manual payment handling
- Lack of trust verification
- No standardized AI marketplace
- Difficult scalability

M2X solves these issues by creating a decentralized AI service ecosystem.

---

# Solution

The platform enables AI agents to:

- Discover AI providers
- Compare available services
- Execute AI workloads
- Pay providers automatically
- Verify execution
- Store receipts
- Maintain provider reputation
- Schedule recurring jobs

---

# Key Features

## AI Agent

- Autonomous planning
- Workflow execution
- Provider selection
- Multi-provider orchestration

---

## AI Marketplace

- Register providers
- Browse services
- Service metadata
- Dynamic pricing
- Capability discovery

---

## Machine-to-Machine Payments

- x402 Protocol
- Escrow payments
- Settlement
- Receipt generation
- Blockchain transactions

---

## Algorand Integration

- Asset management
- Smart contract interaction
- Blockchain verification
- Wallet support

---

## Secure Sandbox

- Isolated execution
- Safe AI workload processing
- Secure compute environment

---

## Reputation Engine

- Provider scoring
- Performance monitoring
- Reliability metrics
- Trust ranking

---

## Scheduler

- Recurring jobs
- Background execution
- Queue management

---

## Job Management

- Pending
- Running
- Completed
- Failed
- Retry support

---

## External Integrations

Supported integrations include:

- Zerion
- Vibekit
- Algokit
- MCP Providers
- External AI APIs

---

# Architecture

```
                         User
                           │
                           ▼
                    FastAPI Backend
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
        ▼                  ▼                  ▼
     AI Agent        Marketplace         Payments
        │                  │                  │
        ▼                  ▼                  ▼
   Planner Engine     Discovery        x402 Protocol
        │
        ▼
 Service Providers
        │
        ▼
 Sandbox Execution
        │
        ▼
 Integrity Check
        │
        ▼
 Receipt Generation
        │
        ▼
 Database
```

---

# Folder Structure

```
backend/
│
├── app/
│   ├── routers/
│   ├── services/
│   ├── agent/
│   ├── bazaar/
│   ├── integrations/
│   ├── payments/
│   ├── workers/
│   ├── x402/
│   ├── a2a/
│   ├── mcp/
│   ├── static/
│   ├── models.py
│   ├── schemas.py
│   ├── db.py
│   ├── config.py
│   ├── security.py
│   └── main.py
│
└── requirements.txt
```

---

# Component Overview

## Routers

| Module | Description |
|----------|------------|
| agent.py | AI Agent APIs |
| marketplace.py | Service Marketplace |
| payments.py | Payment APIs |
| jobs.py | Job execution |
| auth.py | Authentication |
| discovery.py | Service discovery |
| system.py | Health monitoring |
| algokit.py | Algorand APIs |
| vibekit.py | Vibekit integration |
| zerion.py | Wallet analytics |

---

## Agent

Contains the intelligence of the platform.

Responsible for

- Planning
- Decision making
- Workflow execution
- Tool selection

---

## Bazaar

Marketplace engine responsible for:

- Provider registry
- Service listing
- Capability search
- Metadata storage

---

## Services

Business logic including:

- Scheduler
- Ledger
- Receipts
- Reputation
- Jobs
- Cron tasks

---

## Workers

Sandbox execution environment for secure compute jobs.

---

## Integrations

Handles communication with external services.

Current providers include:

- Zerion
- Vibekit
- Algokit

---

## Payments

Implements payment rails and blockchain settlement.

---

## x402

Implements decentralized machine-to-machine payment protocol.

---

## A2A

Agent-to-Agent communication layer.

---

## MCP

Model Context Protocol integration.

---

# Technology Stack

| Category | Technology |
|------------|------------|
| Language | Python 3.11 |
| Backend | FastAPI |
| Database | SQLAlchemy |
| Validation | Pydantic |
| Blockchain | Algorand |
| Payments | x402 |
| Scheduler | APScheduler |
| AI Workflow | Planner + Graph |
| Sandbox | Worker Engine |
| Authentication | API Keys / JWT |
| Storage | Local / Database |
| Logging | Observability |

---

# Workflow

```
User

↓

FastAPI

↓

AI Agent

↓

Planner

↓

Marketplace

↓

Provider Discovery

↓

Provider Selection

↓

Payment

↓

Sandbox Execution

↓

Verification

↓

Receipt Generation

↓

Database

↓

Response
```

---

# Installation

## Clone Repository

```bash
git clone https://github.com/yourusername/m2x-compute-exchange.git

cd m2x-compute-exchange
```

---

## Create Virtual Environment

```bash
python -m venv .venv
```

---

### Windows

```bash
.venv\Scripts\activate
```

### Linux / macOS

```bash
source .venv/bin/activate
```

---

## Install Dependencies

```bash
pip install -r requirements.txt
```

---

## Configure Environment

Create a `.env` file.

Example:

```env
DATABASE_URL=
SECRET_KEY=

ALGORAND_API_KEY=

ZERION_API_KEY=

VIBEKIT_API_KEY=
```

---

## Run Application

```bash
uvicorn app.main:app --reload
```

Application runs at:

```
http://localhost:8000
```

Swagger UI

```
http://localhost:8000/docs
```

---

# Security

The platform implements:

- Authentication
- Authorization
- Integrity Verification
- Secure Receipts
- SHA-256 Hashing
- Blockchain Verification
- Provider Reputation

---

# Future Roadmap

- Multi-chain support
- AI bidding engine
- Dynamic pricing
- Distributed execution
- Federated AI providers
- GPU compute marketplace
- Cross-agent collaboration
- Real-time analytics dashboard

---

# Performance Goals

- Low latency service discovery
- Secure decentralized payments
- Scalable AI compute orchestration
- High availability
- Fault tolerance

---

# Contributing

1. Fork the repository

2. Create a feature branch

```bash
git checkout -b feature-name
```

3. Commit changes

```bash
git commit -m "Added new feature"
```

4. Push changes

```bash
git push origin feature-name
```

5. Open a Pull Request

---

# License

This project is licensed under the MIT License.

---

# Authors

Developed as an AI-native decentralized compute exchange platform for autonomous AI agents and machine-to-machine transactions.

---

# Acknowledgements

- FastAPI
- Python
- Algorand
- x402 Protocol
- Zerion
- Vibekit
- MCP
- SQLAlchemy
- Pydantic

---

## ⭐ If you find this project useful, please consider giving it a star on GitHub.
