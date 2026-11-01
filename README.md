# SAP CDS Intelligence & Automation Platform

> **An AI-powered enterprise platform that accepts natural-language SAP business/data requirements, discovers relevant Core Data Services (CDS) views, tables, and associations in SAP S/4HANA, and recommends: `REUSE` → `EXTEND` → `CREATE`.**

---

## 🎯 Strategic Purpose & Vision

In enterprise SAP S/4HANA transformations, developers and architects frequently duplicate existing data models because discovering standard CDS views (VDM - Virtual Data Model) across thousands of views is notoriously difficult. 

The **SAP CDS Intelligence & Automation Platform** solves this by:
1. Translating natural language business requests into technical entity requirements.
2. Interrogating the SAP Virtual Data Model (VDM: Basic, Composite, Consumption layers) via secure, governed connectors.
3. Formulating a rigorous, deterministic decision:
   - **REUSE:** An existing standard SAP CDS view (e.g., `I_SalesOrderAnalytics`) covers the requirement 100%.
   - **EXTEND:** An existing standard CDS view meets >75% of the requirement and can be extended via CDS View Entity Extension (`EXTEND VIEW ENTITY`) without modifying standard objects.
   - **CREATE:** No standard view provides the foundational model; synthesize a custom CDS View Entity (`Z_...`) following SAP clean-core and naming guidelines.
4. Gating all draft generation, validation, and deployment behind strict **human-in-the-loop governance** and ADT transport mechanisms.

---

## 🔒 Enterprise Guardrails & Safety Architecture

- **Zero Credentials in Code:** No SAP passwords, tokens, or private keys are stored; credentials reside exclusively in uncommitted local `.env`.
- **Strict Connector Abstraction:** Clear separation between platform reasoning and SAP communication protocols (RFC / ADT REST).
- **No Hallucinated SAP Objects:** The system never synthesizes fake tables or imaginary fields as real SAP objects.
- **Dual-Gate Security Guard:** Pre-socket interceptor enforces mandatory human approval (`ReviewStatus.APPROVED`), kill switches (`SAP_DEV_WRITE_ENABLED=True`, `SAP_LIVE_READ_ONLY=False`), customer namespace (`Z*`/`Y*`), and CTS transport validation.
- **Stateful Enqueue Locks:** Implements verified SAP ADT stateful session handling with atomic enqueue lock acquisition and release.

---

## 📊 Proven Capabilities & Scope Boundaries

### ✅ Proven Real SAP DEV Capabilities (Milestones 8 – 13)
* **Real SAP DEV Connectivity**: Authenticated TLS communication via native SAP ADT REST APIs.
* **SAP CDS Discovery**: Live repository search (`GET /sap/bc/adt/repository/informationsystem/search`).
* **SAP CDS Metadata Extraction**: Comprehensive parsing of live DDL sources and annotations.
* **SAP Checkrun Validation**: Live compiler/syntax checks via `POST /sap/bc/adt/checkruns?reporters=abapCheckRun`.
* **Multi-Candidate Semantic Mapping**: 6-level evidence hierarchy (L1 Exact to L6 Missing).
* **Deterministic Decision Engine**: Grounded recommendations for `REUSE` → `EXTEND` → `CREATE`.
* **Human Approval Review Gate**: Multi-role review console with blocking eligibility rules and audit logging.
* **Real SAP DDLS Shell Creation**: Verified `POST /sap/bc/adt/ddic/ddl/sources?corrNr=...`.
* **Real Stateful ADT Locking**: Stateful `POST ...?_action=LOCK&accessMode=MODIFY` enqueue lock acquisition.
* **Real CDS Source Code Upload**: Verified stateful `PUT .../source/main?lockHandle=...&corrNr=...`.
* **Real SAP ADT Activation**: Central `POST /sap/bc/adt/activation?method=activate&preauditRequested=true`.
* **Post-Activation Verification**: Verified `ACTIVE` state via `GET /sap/bc/adt/activation/inactiveobjects` and read-back.

### 🛑 Strictly Out of Scope / Not Implemented
* **QA/PROD Transport Automation**: DEV→QA→PROD transport promotion is strictly blocked.
* **Transport Release**: CTS Workbench transports (e.g. `S3XK911848`) remain open/unreleased.
* **Autonomous Production Deployment**: No direct or un-gated deployment to production environments.

---

## 🏗️ Architecture Overview

```
                         +-----------------------------------+
                         |      Enterprise React Frontend     |
                         |  (13-Step Interactive HITL Studio) |
                         +-----------------+-----------------+
                                           | HTTP / REST
                                           v
                         +-----------------------------------+
                         |         FastAPI Backend           |
                         |   (Pydantic, Async Engine, Core)  |
                         +-----------------+-----------------+
                                           |
                +--------------------------+--------------------------+
                |                                                     |
                v                                                     v
+-------------------------------+                     +-------------------------------+
|    Interactive Services Core  |                     |     SAP Connector Contract    |
| - Gemini Extraction & Variant |                     | (Abstract Protocol / RFC-ADT) |
| - Deterministic VDM Discovery |                     | - search_cds()                |
| - CDS Generation & Validator  |                     | - get_cds_metadata()          |
| - Review & Governance Store   |                     | - search_fields()             |
| - Normalizer & Vocabulary     |                     | - validate_cds()              |
| - Multi-Domain Index Engine   |                     | - activate_cds()              |
+---------------+---------------+                     +---------------+---------------+
                |                                                     |
                v                                                     v
+-------------------------------+                       +-------------------------------+
|     Metadata & Review DB      |                       |       SAP S/4HANA 2023        |
|  (SQLite VDM / Reviews Store) |                       |    S4CORE 108 SP03 / ADT      |
+-------------------------------+                       +-------------------------------+
```

---

## 📁 Repository Structure

```
SAP-CDS-Agent/
├── docs/                        # Architecture, specs, and developer setup guides
│   ├── architecture.md
│   ├── dev-setup.md
│   └── sap-connector-spec.md
├── backend/                     # Python 3.12+ FastAPI backend
│   ├── src/
│   │   ├── api/                 # Endpoints, routers, correlation middleware
│   │   ├── core/                # Custom exceptions, security abstractions
│   │   ├── config.py            # Pydantic Settings configuration
│   │   ├── logging_config.py    # Structured logging with request tracing
│   │   ├── db/                  # SQLite & async session management
│   │   ├── models/              # Pydantic domain models & ORM entities
│   │   ├── sap/                 # SAP ADT Client, Connector, Parser & Models
│   │   └── services/            # Discovery, Gemini, Validation & Review services
│   └── tests/                   # Pytest test suite
├── frontend/                    # Vite + React 18 + TypeScript + Tailwind CSS
│   ├── src/
│   │   ├── components/          # Header, 13-Step Interactive Pipeline Studio
│   │   ├── services/            # Backend API client
│   │   └── types/               # TypeScript models mirrored from backend
└── .env.example                 # Environment configuration template
```

---

## 🚀 Quick Start (Milestone 1)

See [`docs/dev-setup.md`](docs/dev-setup.md) for full setup instructions.

### 1. Backend Setup
```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
pytest tests -v
uvicorn src.main:app --reload --port 8000
```
Backend API will be accessible at: `http://127.0.0.1:8000` (Swagger docs: `http://127.0.0.1:8000/docs`).

### 2. Frontend Setup
```powershell
cd frontend
npm install
npm run dev
```
Frontend UI will be accessible at: `http://localhost:5173`.
"# cds-agent-domain-wise" 
