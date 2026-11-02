# SAP CDS Discovery & Clean Core Agent
## 50 Master Viva & Defense Questions with Comprehensive Technical Answers

**Project Title:** Enterprise SAP CDS Extraction-First Discovery, Strict Classification, HITL Join Configuration, In-Memory Validation & Approved Activation  
**Repository:** `c:\Users\vsrik\Downloads\SAP-CDS-Agent (3)\SAP-CDS-Agent`  
**Target Audience:** Professor, Academic Viva Panel, Enterprise Architecture & Thesis Defense Committee  

---

# TABLE OF CONTENTS
1. [Category 1: SAP Architecture & ADT Integration (Questions 1–10)](#category-1-sap-architecture--adt-integration)
2. [Category 2: Database, Caching & Data Pipeline (Questions 11–18)](#category-2-database-caching--data-pipeline)
3. [Category 3: SAP CDS Taxonomy & Clean Core Architecture (Questions 19–26)](#category-3-sap-cds-taxonomy--clean-core-architecture)
4. [Category 4: Search Algorithms, Table Lineage & Recommendations (Questions 27–34)](#category-4-search-algorithms-table-lineage--recommendations)
5. [Category 5: Human-in-the-Loop (HITL) & Join Configuration (Questions 35–40)](#category-5-human-in-the-loop-hitl--join-configuration)
6. [Category 6: Generative AI, RAG Grounding & Hallucination Defense (Questions 41–45)](#category-6-generative-ai-rag-grounding--hallucination-defense)
7. [Category 7: In-Memory Validation, Security & SAP Deployment (Questions 46–50)](#category-7-in-memory-validation-security--sap-deployment)

---

# Category 1: SAP Architecture & ADT Integration

### Q1. What is ADT, why did you choose it, and how does your application communicate with SAP?
**Answer:**
ADT stands for **ABAP Development Tools**. It is SAP’s modern, REST-based HTTP interface implemented on top of the SAP Internet Communication Framework (ICF) under the root path `/sap/bc/adt/...`. 

We chose ADT instead of standard OData services or RFC/BAPIs because:
- **OData** is designed for business transactional entities (e.g., retrieving purchase orders or business partners).
- **RFC/BAPIs** are legacy binary protocols requiring proprietary native SAP NetWeaver libraries (`sapnwrfc`), which are heavy, platform-dependent, and lack native cloud container portability.
- **ADT** is specifically engineered for repository metadata and source lifecycle: searching the dictionary, retrieving raw ABAP CDS DDL text, executing compiler checkruns, locking transport objects, and activating artifacts.

Our Python backend implements an asynchronous REST client (`SAPADTClient` using `httpx`) that communicates directly with SAP application servers over TLS (HTTPS).

---

### Q2. What exact HTTP endpoints are you calling on SAP?
**Answer:**
Our system communicates with four primary ICF endpoints verified through Eclipse ADT network traces:
1. **Discovery & CSRF Token**: `GET /sap/bc/adt/discovery` and `GET /sap/bc/adt/core/http/reentranceTicket` with header `X-CSRF-Token: Fetch`.
2. **Repository Search**: `GET /sap/bc/adt/repository/informationsystem/search?operation=quickSearch&query={query}*&maxResults=500`.
3. **DDL Source Retrieval**: `GET /sap/bc/adt/ddic/ddl/sources/{name}` and sub-resource `GET /sap/bc/adt/ddic/ddl/sources/{name}/source/main`.
4. **Syntax Check-Run**: `POST /sap/bc/adt/checkruns?reporters=abapCheckRun`.
5. **Package Metadata**: `GET /sap/bc/adt/packages/{package_name}` to extract Application Components.
6. **Inactive Object Registry**: `GET /sap/bc/adt/activation/inactiveobjects` to check object lock status before activation.

---

### Q3. In what data formats does SAP ADT return information, and how do you parse it?
**Answer:**
SAP ADT returns two distinct data formats:
1. **Metadata & Search Results:** Returned as **XML / Atom syndication feeds** (`application/atom+xml` or `text/xml`). We parse these using Python’s `xml.etree.ElementTree`. Each object is represented as an `<objectReference>` element containing attributes such as `name`, `type` (e.g. `DDLS/DF`), `packageName`, `description`, and `uri`.
2. **CDS DDL Source Code:** When querying the `/source/main` sub-resource with `Accept: text/plain`, SAP returns the **pure raw ABAP CDS DDL code** (`define view entity ...`). We parse this DDL using regular expressions and an AST parser in `CDSClassifier` to extract annotations (`@VDM.viewType`), parameters, associations, and projections.

---

### Q4. What filters and query parameters can be passed in an ADT HTTP GET request?
**Answer:**
In `GET /sap/bc/adt/repository/informationsystem/search`, we pass:
- `query`: The search term with wildcard characters (`*` or `?`), e.g., `I_SalesOrder*`.
- `operation`: The search mode, set to `quickSearch`.
- `objectType`: Restricts results strictly to CDS views using `DDLS/DF` (or `TABL/DT` for tables), preventing SAP from searching millions of unrelated classes or programs.
- `packageName` / `package`: Scopes search to a specific ABAP package.
- `maxResults`: Restricts result size (e.g., `500`) to prevent high memory consumption on SAP.
- `sap-client`: Directs the request to a specific SAP tenant client (e.g., `100`).
- `sap-language`: Controls the locale for returned descriptions (e.g., `EN`).

For `GET /sap/bc/adt/ddic/ddl/sources/{name}/source/main`:
- `version`: Set to `active` (production runtime version) or `inactive` (developer draft).
- `If-None-Match`: Sends an ETag header to perform conditional GET caching.

---

### Q5. How does authentication and CSRF token handling work in ADT?
**Answer:**
We support **HTTP Basic Authentication** and **OAuth 2.0 Bearer Tokens**.
For state-modifying requests (like running a checkrun or activating objects):
1. The client sends a handshake request with the HTTP header `X-CSRF-Token: Fetch`.
2. SAP validates credentials and returns a cryptographically signed CSRF token in the response header `X-CSRF-Token: <token>` along with session cookies (`SAP_SESSIONID_*`).
3. For subsequent requests, the client attaches the cached `X-CSRF-Token` and session cookies. Passwords and tokens are encapsulated using Pydantic `SecretStr` to prevent credential exposure in logs.

---

### Q6. What is the difference between active and inactive objects in SAP?
**Answer:**
- **Active Object:** The compiled, runtime-executable version of a development object stored in the active dictionary tables (e.g., `DDLS` active runtime). It is accessible to running applications and external extraction tools.
- **Inactive Object:** A work-in-progress draft stored in inactive tables (e.g., `DDLS` with `AS4LOCAL = 'N'`). Inactive objects cannot be executed by applications and indicate that a developer is currently editing or locking the object.
Our system queries `version=active` to ensure recommendations are based solely on stable runtime code.

---

### Q7. How does the SAP ADT check-run service work?
**Answer:**
SAP ADT provides a compiler check service via `POST /sap/bc/adt/checkruns?reporters=abapCheckRun`.
The client posts an XML payload:
```xml
<chkrun:checkObjectList xmlns:chkrun="http://www.sap.com/adt/checkrun" xmlns:adtcore="http://www.sap.com/adt/core">
    <chkrun:checkObject adtcore:uri="/sap/bc/adt/ddic/ddl/sources/{view_name}" chkrun:version="active"/>
</chkrun:checkObjectList>
```
SAP invokes the ABAP compiler and returns an XML response containing compiler warnings, syntax errors, line numbers, and column offsets.

---

### Q8. Why can't ADT check-run validate unsaved or uncommitted CDS DDL code?
**Answer:**
The SAP ADT check-run endpoint strictly requires an `adtcore:uri` pointing to an **already-persisted repository object** in the SAP database dictionary. It does not accept arbitrary, unsaved source code text in the HTTP payload. 
Checking unsaved code would require first writing a draft object to SAP DEV, which violates our **Zero-Write Security Policy**. Therefore, our system uses an **in-memory deterministic static validator** in Python to check unsaved DDL before creating any objects in SAP.

---

### Q9. What is an ETag and how is conditional GET used with ADT?
**Answer:**
An **ETag (Entity Tag)** is an HTTP response header representing a cryptographic or timestamp hash of a resource version on the SAP server.
When retrieving DDL source via `GET /sap/bc/adt/ddic/ddl/sources/{name}/source/main`, the client caches the ETag. On subsequent requests, it sends `If-None-Match: "{cached-etag}"`.
- If the CDS view is unchanged, SAP returns **HTTP 304 Not Modified** with zero payload bytes, eliminating unnecessary network bandwidth and parsing overhead.
- If the view has been edited in SAP, it returns **HTTP 200 OK** with the new DDL text and a new ETag.

---

### Q10. What is SAP ICF and how does ADT run inside NetWeaver / S/4HANA?
**Answer:**
ICF stands for **Internet Communication Framework**. It is the ABAP kernel subsystem that turns the SAP NetWeaver / S/4HANA application server into an HTTP server and client. Under transaction code `SICF`, SAP manages services such as `/sap/bc/adt`. When an ADT HTTP request arrives, the ICF dispatcher authenticates the user, checks authorization objects (e.g., `S_ADT_RES`), binds the request to an ABAP session, and invokes the appropriate ADT handler class.

---

# Category 2: Database, Caching & Data Pipeline

### Q11. If you can query SAP via ADT, why store data in a local SQLite database?
**Answer:**
An SAP S/4HANA system contains over **86,000 CDS views** and hundreds of thousands of fields. 
1. **Network Latency & Server Load:** Running live fuzzy searches, recursive CTE table lineage, and greedy set-cover field matching across 86,000 views over ADT would take 15 to 45 seconds per query, placing excessive CPU load on the SAP application server.
2. **Instant Search:** By indexing metadata in a local SQLite database with an FTS5 full-text search virtual table, complex multi-domain queries execute in **10 to 25 milliseconds**.
3. **Offline Resilience:** The discovery and recommendation engine can operate even during SAP system maintenance windows.

---

### Q12. What is the size and schema of your SQLite database (`sap_cds.db`)?
**Answer:**
The database file `backend/data/sap_cds.db` is **~410 MB**.
Its core schema consists of:
- `cds_views` (86,300 rows): `cds_name`, `label`, `domain`, `grain`, `source_code`, `vdm_view_type`, `is_parameterized`.
- `cds_fields` (30,103 rows): `id`, `cds_name`, `fieldname`, `data_element`, `data_type`, `length`.
- `cds_associations` (133,587 rows): `cds_name`, `target_entity`, `association_name`, `cardinality`, `on_condition`.
- `cds_joins` (23,353 rows): `cds_name`, `left_entity`, `right_entity`, `join_type`, `left_fields`.
- `cds_dependencies` (256 rows): Dependency graph mapping parent CDS views to base DDIC tables.
- `cds_views_fts` (86,300 documents): SQLite FTS5 full-text index across `cds_name`, `label`, `package`, `application_component`, and `domain`.

---

### Q13. How did you index and classify all 86,300 CDS views?
**Answer:**
We built an offline catalog migration pipeline that read all 86,298 populated DDL source records from the repository and executed `CDSClassifier`:
1. It inspected the `@VDM.viewType` annotation to classify views as `#BASIC`, `#COMPOSITE`, or `#CONSUMPTION`.
2. It scanned for the syntax pattern `with parameters` to flag parameterized views.
3. The catalog was migrated with dedicated indexed columns:
   ```sql
   ALTER TABLE cds_views ADD COLUMN vdm_view_type TEXT;
   ALTER TABLE cds_views ADD COLUMN is_parameterized INTEGER DEFAULT 0;
   CREATE INDEX idx_cds_views_vdm ON cds_views (vdm_view_type, is_parameterized);
   ```
This yielded **27,834 verified non-parameterized BASIC views**.

---

### Q14. What is SQLite FTS5 and how does your project use it?
**Answer:**
SQLite **FTS5** (Full-Text Search 5) is an extension module that provides full-text indexing using an inverted index structure with BM25 relevance ranking.
In our project:
- We index `cds_name`, `label`, `package`, `application_component`, and `domain`.
- When a user inputs natural language queries like `"sales order net amount by customer"`, FTS5 scores matching views using BM25 token relevance, enabling sub-millisecond retrieval of candidate views.

---

### Q15. How do you detect and prevent "metadata drift" between the local cache and live SAP?
**Answer:**
We implement three consistency mechanisms:
1. **Conditional GET with ETags**: On DDL fetch, we send `If-None-Match`. A 304 response confirms the cache is fresh.
2. **Deterministic SHA-256 Source Hashing**: We compute `compute_source_hash(source)` on normalized DDL (`\r\n` converted to `\n`, whitespace trimmed). If the live hash differs from the catalog hash, the local cache is updated immediately.
3. **1-Hour TTL on Authoritative Views**: The extraction catalog `I_DataExtractionEnabledView` is cached with a 1-hour Time-to-Live (TTL), automatically refreshing periodically.

---

### Q16. Why do you normalize line endings (`\r\n` to `\n`) before hashing source code?
**Answer:**
Windows development environments and SAP GUI / Eclipse ADT often use different line termination standards (`\r\n` CRLF on Windows vs `\n` LF on SAP NetWeaver Linux application servers). SAP application servers also strip trailing whitespace and newlines upon compile. If raw strings were hashed directly, identical DDL code would produce completely different SHA-256 hashes, causing false-positive cache invalidation and checkrun rejections. Normalizing line endings ensures cross-platform determinism.

---

### Q17. How is `I_DataExtractionEnabledView` populated and maintained?
**Answer:**
`I_DataExtractionEnabledView` is a standard SAP CDS view that lists all CDS views released by SAP for data extraction and Change Data Capture (CDC).
Our backend queries this view via ADT/OData or catalog sync and extracts:
- `DataExtractionViewName`
- `DataExtractionViewDescription`
- `IsSAPReleasedView`
- `DeltaChgDataCaptureIsSupported`
There are **6,102 verified views** in this table. These records are cached in memory and indexed in SQLite.

---

### Q18. What is the query execution time of your database versus a live ADT call?
**Answer:**
- **Local SQLite Index (FTS5 + B-Tree indexes):** **5 to 20 milliseconds** per search query.
- **Live ADT Information System Search:** **800 to 2,500 milliseconds** per HTTP round-trip over the network.
- **Deep Live Recursive Traversal:** **15 to 45 seconds** if executed live over ADT across multiple hierarchy levels.

---

# Category 3: SAP CDS Taxonomy & Clean Core Architecture

### Q19. What is the SAP Virtual Data Model (VDM)?
**Answer:**
The **Virtual Data Model (VDM)** is SAP’s architectural framework that structures CDS views into distinct layers to guarantee semantic consistency, reusability, and upgrade stability across S/4HANA:
1. **Basic / Interface Views (`I_*`):** Low-level views built directly on database tables.
2. **Composite Views:** Intermediate views combining Basic views, business logic, and associations.
3. **Consumption Views (`C_*`):** Top-level analytical or UI-oriented views consumed by Fiori apps or analytical engines.

---

### Q20. What is the exact definition of a BASIC CDS view, and why does this project recommend ONLY BASIC views?
**Answer:**
A **BASIC view** (annotated with `@VDM.viewType: #BASIC`) represents a single business entity directly modeled on base database tables without complex aggregations or multi-grain business joins.

**Why BASIC only?**
1. **Clean Extraction Granularity:** Downstream ETL tools (SAP Datasphere, Azure Data Factory, Databricks) require raw transactional grains. Composite and Consumption views introduce hidden filters, pre-aggregations, or cartesian joins that distort data warehouse metrics.
2. **Clean Core Compliance:** SAP Clean Core guidelines mandate building custom analytical extractions on top of released Basic Interface Views (`I_*`), never on volatile Consumption views.

---

### Q21. How do you programmatically detect whether a CDS view is parameterized?
**Answer:**
In `CDSClassifier.is_parameterized`, we perform:
1. **AST / Regex Syntax Scanning:** We check the DDL source for the clause:
   `with parameters\s+([pP]_\w+|\w+)\s*:`
2. **Catalog Metadata Verification:** In the SQLite catalog, `is_parameterized = 1` indicates that mandatory input parameters are declared.
If a view contains parameters (e.g. `P_ExchangeRateDate`, `P_DisplayCurrency`), it is flagged as parameterized.

---

### Q22. Why must parameterized CDS views be strictly excluded from data extraction?
**Answer:**
Data extraction engines (such as SAP CDC, SAP Data Intelligence, and SLT) perform automated, unattended batch and streaming delta replication. Parameterized CDS views cannot be queried with a standard `SELECT * FROM View` because the database engine requires explicit parameter bindings at runtime. Without parameter values passed in every query, the database query fails. Therefore, only non-parameterized views can serve as automated extraction sources.

---

### Q23. What is SAP Clean Core, and how does this project enforce it?
**Answer:**
**SAP Clean Core** is SAP’s architectural strategy to keep the core ERP system untouched during customizations, ensuring seamless cloud upgrades:
1. **Zero Modifications:** Never alter standard SAP tables (`VBAK`, `EKKO`) or standard CDS views.
2. **Stable Public APIs:** Only build on SAP-released Basic Interface Views (`I_*`).
3. **Customer Namespace:** All generated custom views must start with `Z` or `Y` (e.g., `ZI_SalesOrder_Ext`).
4. **Zero-Footprint Reuse First:** If an existing standard view in `I_DataExtractionEnabledView` meets the requirement, reuse it directly without generating custom code.

---

### Q24. What annotations are mandatory for data extraction and CDC in an ABAP CDS View Entity?
**Answer:**
A valid extraction CDS View Entity must include:
1. `@Analytics.dataExtraction.enabled: true`: Enables the CDS view for extraction via the Operational Data Provisioning (ODP) framework.
2. `@Analytics.dataExtraction.delta.byElement.detectDeletedRecords: true`: Enables tracking of deleted rows.
3. `@Analytics.dataExtraction.delta.byElement.date: 'ChangeDate'` (or timestamp): Designates the delta watermark field for Change Data Capture (CDC).
4. `@AccessControl.authorizationCheck: #CHECK` (or `#NOT_REQUIRED`): Specifies authorization checks.
5. `@EndUserText.label: '...'`: Human-readable description.

---

### Q25. What is the difference between `define view entity` and obsolete `define view`?
**Answer:**
- **Obsolete `define view` (CDS v1 / Classic):** Requires an `@AbapCatalog.sqlViewName: 'ZSQL_VIEW'` annotation. It generates two separate objects in the database dictionary: a CDS DDL entity and an underlying classical SQL View. This causes dual maintenance, activation delays, and architectural overhead.
- **Modern `define view entity` (CDS v2):** Introduced in SAP S/4HANA 2020. It creates a single, native database object with no SQL view append structure. It offers faster activation, stricter type checking, and native Clean Core compliance. Our generator strictly emits `define view entity`.

---

### Q26. What rules govern SAP customer object naming?
**Answer:**
Under SAP naming conventions, enforced in `SAPADTClient.validate_sap_object_name`:
1. The name must start with `Z` or `Y` (e.g., `ZI_*`, `ZC_*`, `ZE_*`).
2. Length must be between 3 and 30 characters.
3. Only uppercase alphanumeric characters and underscores are permitted (`^[ZY][A-Z0-9_]{2,29}$`).

---

# Category 4: Search Algorithms, Table Lineage & Recommendations

### Q27. How does domain-based CDS discovery work?
**Answer:**
When a user selects one or more business domains (e.g., `Sales`, `Finance`, `Procurement`):
1. **Package / Component Filtering:** Candidate views are filtered by ABAP Package and Application Component (e.g., `SD` for Sales, `FI`/`CO` for Finance, `MM` for Procurement).
2. **Strict BASIC Scoping:** Views outside the selected domain or non-BASIC views are excluded.
3. **Multi-Domain Union:** If both `Sales` and `Finance` are selected, the engine queries both domain partitions, removes duplicates by uppercase CDS name, and preserves domain tags.
4. Unverified candidates are categorized as `Unclassified` rather than guessed.

---

### Q28. How does Table Lineage search work? If a user enters table `VBAK`, how do you find its CDS views?
**Answer:**
We execute a **Recursive Common Table Expression (CTE)** query in SQLite over `cds_dependencies` and `cds_sources`:
1. **Anchor Step:** Locate all CDS views whose source code directly queries `from vbak`.
2. **Recursive Step:** Traverse upward to find parent views that select from or join with these views.
3. **Strict Filter Enforcement:** At every recursion step, we enforce:
   `vdm_view_type = 'BASIC' AND (is_parameterized = 0 OR is_parameterized IS NULL)`
4. The recursion returns only verified non-parameterized BASIC views (such as `I_SalesOrder`). Any COMPOSITE or PARAMETERIZED views are pruned from the lineage graph.

---

### Q29. How does the system calculate field coverage percentage?
**Answer:**
Field coverage is calculated as:
$$\text{Field Coverage} = \frac{|\text{Matched Required Fields}|}{|\text{Total Required Fields}|} \times 100\%$$
- Each required business field is compared against the technical field names and descriptions in `cds_fields`.
- If a single view matches 100% of the required fields, coverage is 100%.
- If a single view covers 70% and another covers 30%, their combined coverage is evaluated through pre-verified joins.

---

### Q30. What is Outcome A versus Outcome B in your Recommendation engine?
**Answer:**
- **Outcome A (Existing Extraction Available):** A verified, non-parameterized BASIC view exists in `I_DataExtractionEnabledView` that satisfies 100% of the required fields. The UI displays **`Existing Extraction-Enabled CDS Available`** with button **`SELECT EXISTING CDS →`**. The user directly approves reuse without creating custom code (**Zero Clean-Core Footprint**).
- **Outcome B (Custom Wrapper Required):** No single extraction-enabled view provides 100% field coverage (e.g., missing fields require joining multiple views, or the best candidate lacks `@Analytics.dataExtraction.enabled`). The UI displays **`CUSTOM WRAPPER REQUIRED`** and activates the Human-in-the-Loop join and field selection workbench.

---

### Q31. What information is displayed in Part A of Outcome B?
**Answer:**
Part A ("Why is a Custom Wrapper Required?") provides dynamic technical evidence:
1. An explicit explanation of why standard extraction views failed (e.g., *"Standard extraction view covers only 4 of 6 required fields"*).
2. A list of missing fields highlighted in amber/red.
3. A list of eligible BASIC source views capable of providing the missing fields.
4. Combined coverage calculations.

---

### Q32. What algorithm finds the best combination of views when no single view has 100% coverage?
**Answer:**
We use a **Greedy Set-Cover Algorithm constrained by association graph connectivity**:
1. Select the candidate view with the highest individual field coverage.
2. Identify remaining missing fields.
3. Query `cds_associations` and `cds_joins` to find related BASIC views that share foreign-key association edges with the primary view.
4. Choose the connected view that covers the largest number of remaining missing fields.
5. Repeat until 100% coverage is reached or no further connected views exist.

---

### Q33. Why not recommend a COMPOSITE view if it already has all required fields?
**Answer:**
While a COMPOSITE view might contain all required fields, recommending it for data extraction introduces severe operational risks:
1. It frequently performs inner joins and group-by aggregations that hide underlying item-level transactional rows.
2. It cannot be reliably enabled for delta CDC extraction because delta capture requires a continuous primary key watermark on base tables.
3. Clean Core architecture requires building customer extraction wrappers directly on top of BASIC views.

---

### Q34. How does the system handle "Unclassified" domain CDS views?
**Answer:**
If a CDS view resides in a custom or local package (`$TMP`) where the Application Component is missing from SAP package metadata:
- The system labels the view `Domain: Unclassified`.
- It does not guess or assign a random domain based on view name.
- It displays a notice stating why the domain could not be verified, maintaining strict data integrity.

---

# Category 5: Human-in-the-Loop (HITL) & Join Configuration

### Q35. Why is there a Human-in-the-Loop (HITL) gate for join configuration instead of letting AI auto-join?
**Answer:**
In enterprise SAP databases, automated join generation carries severe risks:
1. **Cartesian Product Explosion:** Joining a header view (`I_SalesOrder`) to an item view (`I_SalesOrderItem`) requires exact composite keys (`SalesOrder = SalesOrder`). A missing join key produces a cartesian explosion, generating millions of invalid duplicate rows and crashing database memory.
2. **Semantic Ambiguity:** Joining on `Customer` could mean `SoldToParty`, `ShipToParty`, or `Payer`. Only a human business engineer can confirm the correct business semantics.
The HITL gate forces human review and sign-off on join keys before code synthesis.

---

### Q36. What controls does the user have on the HITL Join Configuration page?
**Answer:**
Directly on the Recommendation page (Part D), the engineer can:
1. Select **Left View** and **Right View** from the selected sources.
2. Select **Left Field** and **Right Field** from dropdowns populated with real verified fields.
3. Choose the **Join Type**: `INNER JOIN` or `LEFT OUTER JOIN`.
4. Directly edit or fine-tune the `ON` condition expression.
5. Use **Quick-Fill Pills** for standard keys (`SalesOrder`, `Customer`, `CompanyCode`, `Material`).
6. View real-time validation badges: `✓ SAP-Verified Relationship` vs `⚡ User-Proposed Relationship`.

---

### Q37. What is the difference between an "SAP-Verified Relationship" and a "User-Proposed Relationship"?
**Answer:**
- **`✓ SAP-Verified Relationship` (Emerald Badge):** The join condition matches an authoritative association already declared in SAP’s standard metadata catalog (`cds_associations`). The foreign-key relationship and cardinality have been pre-validated by SAP.
- **`⚡ User-Proposed Relationship` (Blue Badge):** The user specified a custom join condition not present in SAP’s standard associations. The system warns the user to verify cardinality and indexing.

---

### Q38. When should an engineer use `INNER JOIN` versus `LEFT OUTER JOIN` in a CDS wrapper?
**Answer:**
- **`INNER JOIN`:** Used when every record in the left entity must have a matching record in the right entity (e.g., an Order Item must belong to an Order Header). Records without matches are excluded.
- **`LEFT OUTER JOIN`:** Used when right-entity details are optional (e.g., joining Sales Order to Customer Address or Delivery Notes). If an address does not exist, the sales order record is still retained with NULL address fields. In data extraction, `LEFT OUTER JOIN` is generally preferred to prevent data loss.

---

### Q39. What is the Output Schema Field Selection Table (Part C)?
**Answer:**
Part C renders an interactive table of all candidate fields across the selected views:
- Columns: `Select Checkbox`, `Technical Field Name`, `Source CDS View`, `Data Type`, `Length`, `Primary Key`, and `Description/Lineage`.
- Quick-action buttons: `[ Select All ]`, `[ Required Only ]`, `[ Clear ]`.
- Only fields selected with active checkboxes are included in the `$projection` list of the generated wrapper CDS view.

---

### Q40. What happens if only a single source CDS view is selected?
**Answer:**
If the engineer selects only one view (e.g. an existing non-extraction BASIC view that covers all fields), the HITL join section automatically simplifies, displaying:
*"Single CDS wrapper selected. All fields will be projected directly without joins."*
No join rows are displayed, preventing unnecessary join configuration.

---

# Category 6: Generative AI, RAG Grounding & Hallucination Defense

### Q41. What LLM model is used, and what is its exact role in the architecture?
**Answer:**
We use Google’s **Gemini** model via the official Google GenAI SDK.
The LLM does **not** make decisions about database schemas or discover tables on its own. It serves strictly as a **deterministic code synthesizer and translation engine**:
1. Translating natural language business requirements into required field names.
2. Synthesizing valid S/4HANA ABAP CDS View Entity DDL (`define view entity ZI_*`) based strictly on RAG-grounded context provided by the backend.

---

### Q42. How do you prevent the LLM from hallucinating non-existent tables or fields?
**Answer:**
We enforce strict **RAG (Retrieval-Augmented Generation) Grounding**:
1. **Source DDL Injection:** The system prompt receives the exact, complete, unedited source DDL of only the user-selected CDS views.
2. **Schema Whitelist:** The prompt includes the exact list of approved field names and data types from the HITL field selection.
3. **Approved Join Injection:** The prompt includes the approved join conditions.
4. **Negative Constraints:** The system prompt explicitly commands:
   *"Do NOT invent fields, entities, or associations. Only project fields explicitly listed in the source DDL. Use only define view entity syntax."*
5. **Post-Generation AST Validator:** Any hallucinated field is immediately caught by our deterministic static validator before code reaches the user.

---

### Q43. What is the 3-attempt Validation Correction Loop?
**Answer:**
Implemented in `backend/src/services/gemini_service.py` (`generate_wrapper_with_correction_loop`):
- **Attempt 1:** The LLM synthesizes the initial CDS DDL.
- **Static Validation:** The code is evaluated by `CDSGenerationValidator`.
- **Correction Feedback:** If validation fails, the exact line numbers and compiler error descriptions are formatted into a correction prompt:
  *"Validation failed on line 12: Field XYZ does not exist in source view I_SalesOrder. Please correct only this error while preserving all other code."*
- **Attempts 2 & 3:** The LLM corrects the code, which is re-validated.
- **Cap:** The loop halts after **maximum 3 attempts** to prevent infinite execution and token exhaustion.

---

### Q44. What exact prompt structure is sent to Gemini for wrapper generation?
**Answer:**
The prompt contains 5 structured blocks:
1. **System Persona:** Senior SAP ABAP CDS Architect enforcing Clean Core standards.
2. **Target View Specifications:** Customer name (e.g., `ZI_SalesOrder_Extract`), description, and mandatory annotations (`@Analytics.dataExtraction.enabled: true`).
3. **Source CDS Definitions:** Full raw DDL text of selected views.
4. **Approved Joins & Projections:** Exact join clauses and selected field lists.
5. **Formatting Rules:** Output only raw DDL code without markdown formatting or conversational commentary.

---

### Q45. Why is LLM synthesis better than simple string template generation?
**Answer:**
While simple views can use templates, enterprise wrappers often require complex syntactic variations:
- Handling aliased fields to resolve naming collisions across joined views (e.g., `Header.CreationDate as OrderCreationDate`, `Item.CreationDate as ItemCreationDate`).
- Formatting complex multi-clause `ON` conditions with projections (`$projection.SalesOrder = _Item.SalesOrder`).
- Constructing type-safe cast expressions (`cast( ... as abap.char(10) )`) when joined types differ.
The LLM handles these syntactic complexities while remaining strictly bounded by RAG grounding and static validation.

---

# Category 7: In-Memory Validation, Security & SAP Deployment

### Q46. How does your 22-rule in-memory static validator work?
**Answer:**
Implemented in `backend/src/services/cds_generation_validator.py` (`CDSGenerationValidator`), it executes 22 deterministic AST and regex checks:
1. **Header Checks:** Validates `define view entity`, customer naming (`Z*`/`Y*`), and labels.
2. **Annotation Checks:** Verifies `@Analytics.dataExtraction.enabled: true` and CDC delta watermark elements.
3. **Syntax Checks:** Rejects legacy SQL view annotations (`@AbapCatalog.sqlViewName`), verifies key field placement, and checks balanced braces/parentheses.
4. **Field Integrity Checks:** Confirms that all projected fields exist in the source views and verifies join clauses use `$projection` prefixes.
5. **Clean Core Checks:** Rejects direct queries to physical database tables (`select from vbak`), ensuring queries build on CDS views.

---

### Q47. What is the Pre-Socket Zero-Write Security Interceptor?
**Answer:**
In `SAPADTClient._assert_zero_write_policy`, our client inspects every HTTP call **before opening a network socket**:
1. **Method Check:** Blocks any `PUT`, `DELETE`, or `PATCH`.
2. **Allowlist Scoping:** Only allowlists read-only paths (`/informationsystem/search`, `/ddic/ddl/sources/{name}`, `/checkruns?reporters=abapCheckRun`).
3. **Kill-Switch:** Refuses socket connections unless `SAP_LIVE_READ_ONLY=true` (or explicit `SAP_DEV_WRITE_ENABLED=true`).
This guarantees the agent can never accidentally write to or corrupt an SAP production system.

---

### Q48. What is a CTS Workbench Transport Request, and what regex format is enforced?
**Answer:**
In SAP, the **Change and Transport System (CTS)** tracks and transports development objects across system landscapes (DEV $\rightarrow$ QAS $\rightarrow$ PRD).
A Workbench Transport Request is strictly validated using regex:
`^[A-Z0-9]{3}K[0-9]{6}$`
- **3 alphanumeric characters:** SAP System ID (e.g., `S4H`, `DEV`, `YAW`).
- **Literal 'K':** Designates a workbench transport request.
- **6 digits:** Sequential request number (e.g., `900123`).
Example: `S4HK900123`. Empty strings, arbitrary test text, or malformed numbers are rejected before deployment.

---

### Q49. What are the end-to-end steps to activate the generated CDS view in SAP?
**Answer:**
The deployment pipeline follows a strict 5-stage gate in `backend/src/services/review_service.py`:
1. **Validation Gate:** The wrapper DDL must pass all 22 static validation checks.
2. **Approval Gate:** A human administrator reviews the review package and grants approval (`status = APPROVED`).
3. **Transport Validation:** A verified CTS Workbench Transport Request is supplied.
4. **ADT Dispatch & Activation:** The client creates the DDLS entity in SAP DEV under the transport request and triggers SAP activation via `/sap/bc/adt/activation`.
5. **Post-Activation Verification:** The client re-queries `I_DataExtractionEnabledView` to confirm that SAP has officially registered the new view entity for extraction.

---

### Q50. How is direct reuse (Outcome A) handled compared to custom wrapper creation (Outcome B)?
**Answer:**
- **In Outcome A (Direct Reuse):**
  1. The user clicks **`SELECT EXISTING CDS →`** on the Recommendation page.
  2. The system sets `isReuseFlow = true` and navigates directly to **Mapping (`activeStep = 5`)**.
  3. In Mapping, the action button reads: **`Approve Direct CDS Reuse (Zero Clean-Core Footprint) →`**.
  4. Clicking it bypasses code generation, validation, and SAP creation, navigating directly to the completion screen (`activeStep = 12`). Zero objects are created in SAP.
- **In Outcome B (Custom Wrapper):**
  1. The user selects multiple views and configures joins and fields.
  2. Clicking **`CONFIRM CDS SELECTION AND JOIN CONFIGURATION`** navigates to Mapping.
  3. The workflow proceeds through **CDS Generation (Step 4)** $\rightarrow$ **Validation (Step 5)** $\rightarrow$ **Review & Approval (Step 6)** $\rightarrow$ **Live SAP Activation (Step 7)**.

---

## Quick Reference Summary Table for Oral Defense

| Architecture Layer | Core Technology / Specification | Key Number / Standard |
|---|---|---|
| **SAP Integration Protocol** | SAP ADT (ABAP Development Tools) via ICF | REST over TLS / HTTPS |
| **Catalog Database** | SQLite with FTS5 Full-Text Search | `sap_cds.db` (~410 MB) |
| **Total Indexed Catalog** | SAP S/4HANA CDS Views & Associations | **86,300 views**, **133,587 associations** |
| **Verified Basic Views** | `@VDM.viewType: #BASIC` (Non-Parameterized) | **27,834 views** |
| **Standard Extraction Views**| `I_DataExtractionEnabledView` | **6,102 verified views** |
| **LLM Model** | Google Gemini (via official GenAI SDK) | Constrained RAG Grounding |
| **Static Code Validation** | `CDSGenerationValidator` (Python AST/Regex) | **22 deterministic rules** |
| **Correction Loop** | Automatic LLM Compiler Feedback Loop | **Capped at 3 attempts max** |
| **CTS Transport Format** | Regex `^[A-Z0-9]{3}K[0-9]{6}$` | e.g. `S4HK900123` |
| **Security Interceptor** | Pre-Socket Zero-Write Policy | `SAP_LIVE_READ_ONLY=true` |
