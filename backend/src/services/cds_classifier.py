"""Authoritative Reusable SAP CDS Classification Service.

Enforces strict classification of every CDS view entity based exclusively on
actual SAP DDL source text and verified SAP metadata.

Classifications:
- BASIC: Verified `@VDM.viewType: #BASIC` and strictly NO parameters.
- COMPOSITE: Verified `@VDM.viewType: #COMPOSITE`.
- CONSUMPTION: Verified `@VDM.viewType: #CONSUMPTION`.
- PARAMETERIZED: Contains `with parameters` (regardless of base view type).
- UNKNOWN: Missing `@VDM.viewType` annotation or insufficient evidence.

Under Clean Core and Master Implementation rules:
- Names (such as I_* prefix) or natural language descriptions MUST NEVER be used alone to classify.
- A view is accepted as BASIC only when verified.
- COMPOSITE, CONSUMPTION, PARAMETERIZED, and UNKNOWN views are strictly excluded from BASIC acceptance.
"""

import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from pydantic import BaseModel, Field

from src.logging_config import logger
from src.models.domain import CDSClassificationType


class ClassificationResult(BaseModel):
    """Authoritative classification result for a single CDS view entity."""
    cds_name: str
    classification: CDSClassificationType
    is_basic: bool = False
    is_parameterized: bool = False
    raw_vdm_type: Optional[str] = None
    parameters: List[str] = Field(default_factory=list)
    evidence: str = ""
    ddl_retrievable: bool = False
    source_snippet: Optional[str] = None


class CDSClassificationService:
    """Singleton-capable authoritative classifier for SAP CDS views."""

    def __init__(self, db_path: Optional[str] = None):
        if db_path:
            self.db_path = Path(db_path)
        else:
            self.db_path = Path(__file__).resolve().parent.parent.parent / "data" / "sap_cds.db"
            if not self.db_path.exists():
                self.db_path = Path(__file__).resolve().parent.parent / "data" / "sap_cds.db"

    def _get_db(self) -> Optional[sqlite3.Connection]:
        if self.db_path and self.db_path.exists():
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            return conn
        return None

    @staticmethod
    def strip_comments(ddl: str) -> str:
        """Strips single-line and multi-line comments from DDL source code."""
        if not ddl:
            return ""
        ddl = re.sub(r"/\*[\s\S]*?\*/", "", ddl)
        ddl = re.sub(r"//.*$", "", ddl, flags=re.MULTILINE)
        ddl = re.sub(r"--.*$", "", ddl, flags=re.MULTILINE)
        return ddl

    @classmethod
    def extract_parameters(cls, ddl_source: str) -> List[str]:
        """Extracts declared parameter names if present."""
        clean = cls.strip_comments(ddl_source)
        # Match 'with parameters ... as select' or 'with parameters ... {'
        param_match = re.search(
            r"\bwith\s+parameters\s+([\s\S]*?)(?=\bas\s+select\b|\{)",
            clean,
            re.IGNORECASE,
        )
        if not param_match:
            return []
        param_block = param_match.group(1).strip()
        # Extract individual parameter identifiers (e.g., P_KeyDate : vdm_v_key_date)
        raw_params = re.findall(r"([a-zA-Z0-9_]+)\s*:", param_block)
        return [p.strip() for p in raw_params if p.strip()]

    @classmethod
    def classify_source(
        cls,
        ddl_source: str,
        view_name: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ClassificationResult:
        """Strictly classifies CDS DDL source code against authoritative rules."""
        cds_name = (view_name or "").strip().upper() or "UNKNOWN_CDS"
        if not ddl_source or not ddl_source.strip():
            return ClassificationResult(
                cds_name=cds_name,
                classification=CDSClassificationType.UNKNOWN,
                is_basic=False,
                is_parameterized=False,
                evidence="No DDL source code available for classification.",
                ddl_retrievable=False,
            )

        clean_ddl = cls.strip_comments(ddl_source)

        # 1. Parameter Detection: Any presence of `with parameters`
        has_parameters = bool(re.search(r"\bwith\s+parameters\b", clean_ddl, re.IGNORECASE))
        parameters = cls.extract_parameters(ddl_source) if has_parameters else []

        # 2. Extract VDM View Type Annotation (supports both flat @VDM.viewType and nested @VDM: { viewType: #... })
        vdm_match = re.search(
            r"@VDM(?:\.viewType|\s*:\s*\{[^}]*viewType)\s*:\s*#([A-Za-z0-9_]+)",
            clean_ddl,
            re.IGNORECASE,
        )
        raw_vdm = vdm_match.group(1).upper() if vdm_match else None

        # 3. If raw_vdm is not in DDL, check metadata dictionary if provided
        if not raw_vdm and metadata:
            meta_vdm = metadata.get("vdm_type") or metadata.get("layer") or metadata.get("view_type")
            if meta_vdm:
                m_up = str(meta_vdm).strip().upper().replace("#", "")
                if m_up in ("BASIC", "COMPOSITE", "CONSUMPTION"):
                    raw_vdm = m_up

        snippet = ddl_source[:300].strip()

        # 4. Acceptance Rule: Parameterized views are strictly excluded
        if has_parameters:
            evidence = (
                f"Parameterized CDS View: Declared parameters: {', '.join(parameters) if parameters else 'yes'}. "
                f"(Base VDM Type: #{raw_vdm if raw_vdm else 'UNKNOWN'}). Excluded from BASIC acceptance."
            )
            return ClassificationResult(
                cds_name=cds_name,
                classification=CDSClassificationType.PARAMETERIZED,
                is_basic=False,
                is_parameterized=True,
                raw_vdm_type=raw_vdm,
                parameters=parameters,
                evidence=evidence,
                ddl_retrievable=True,
                source_snippet=snippet,
            )

        # 5. Non-parameterized classification by verified @VDM.viewType
        if raw_vdm == "BASIC":
            return ClassificationResult(
                cds_name=cds_name,
                classification=CDSClassificationType.BASIC,
                is_basic=True,
                is_parameterized=False,
                raw_vdm_type="BASIC",
                evidence="Verified @VDM.viewType: #BASIC in DDL source. Non-parameterized.",
                ddl_retrievable=True,
                source_snippet=snippet,
            )
        elif raw_vdm == "COMPOSITE":
            return ClassificationResult(
                cds_name=cds_name,
                classification=CDSClassificationType.COMPOSITE,
                is_basic=False,
                is_parameterized=False,
                raw_vdm_type="COMPOSITE",
                evidence="Verified @VDM.viewType: #COMPOSITE in DDL source. Excluded from BASIC acceptance.",
                ddl_retrievable=True,
                source_snippet=snippet,
            )
        elif raw_vdm == "CONSUMPTION":
            return ClassificationResult(
                cds_name=cds_name,
                classification=CDSClassificationType.CONSUMPTION,
                is_basic=False,
                is_parameterized=False,
                raw_vdm_type="CONSUMPTION",
                evidence="Verified @VDM.viewType: #CONSUMPTION in DDL source. Excluded from BASIC acceptance.",
                ddl_retrievable=True,
                source_snippet=snippet,
            )
        else:
            return ClassificationResult(
                cds_name=cds_name,
                classification=CDSClassificationType.UNKNOWN,
                is_basic=False,
                is_parameterized=False,
                raw_vdm_type=None,
                evidence="Missing or unverified @VDM.viewType annotation. Excluded from BASIC acceptance.",
                ddl_retrievable=True,
                source_snippet=snippet,
            )

    def classify_view(
        self,
        view_name: str,
        conn: Optional[sqlite3.Connection] = None,
    ) -> ClassificationResult:
        """Classifies a view by loading its authoritative DDL source from the database."""
        clean_name = view_name.strip().upper()
        should_close = False
        db = conn
        if db is None:
            db = self._get_db()
            should_close = True

        if db is None:
            return ClassificationResult(
                cds_name=clean_name,
                classification=CDSClassificationType.UNKNOWN,
                is_basic=False,
                is_parameterized=False,
                evidence="Database connection unavailable to retrieve DDL source.",
                ddl_retrievable=False,
            )

        try:
            cur = db.cursor()
            cur.execute("SELECT cds_name, source_code FROM cds_views WHERE UPPER(cds_name) = ?", (clean_name,))
            row = cur.fetchone()
            if not row or not row["source_code"]:
                return ClassificationResult(
                    cds_name=clean_name,
                    classification=CDSClassificationType.UNKNOWN,
                    is_basic=False,
                    is_parameterized=False,
                    evidence=f"DDL source code not found in catalog for view '{clean_name}'.",
                    ddl_retrievable=False,
                )

            ddl_source = row["source_code"]
            return self.classify_source(ddl_source, view_name=clean_name)
        finally:
            if should_close and db is not None:
                db.close()

    @classmethod
    def is_verified_basic(cls, result: ClassificationResult) -> bool:
        """Strict acceptance predicate: returns True ONLY when verified BASIC and not parameterized."""
        return result.classification == CDSClassificationType.BASIC and result.is_basic and not result.is_parameterized

    @classmethod
    def is_wrapper_source_eligible(
        cls,
        view_name: str,
        ddl_source: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str]:
        """Wrapper Source Eligibility (Part 4.B):

        A CDS view must be:
        - BASIC (verified).
        - Non-parameterized.
        - Verified in SAP metadata.
        - Retrievable source DDL.
        Note: Extraction does NOT need to be enabled for wrapper sources!
        """
        res = cls.classify_source(ddl_source, view_name=view_name, metadata=metadata)
        if not res.ddl_retrievable:
            return False, f"Source DDL for '{view_name}' is not retrievable."
        if res.is_parameterized:
            return False, f"View '{view_name}' has parameters ({', '.join(res.parameters)}). Excluded from wrapper source."
        if not res.is_basic:
            return False, f"View '{view_name}' is not a verified BASIC view (Classified as: {res.classification.value}). Excluded from wrapper source."
        return True, "Verified BASIC non-parameterized view eligible as wrapper source."

    @classmethod
    def is_final_recommendation_eligible(
        cls,
        view_name: str,
        ddl_source: str,
        extraction_records: Dict[str, Any],
        required_fields: List[str],
        field_metadata: List[Dict[str, Any]],
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """Final Recommendation Eligibility (Part 3 / Part 4.A):

        A CDS view must be:
        - BASIC (verified).
        - Non-parameterized.
        - Extraction enabled according to authoritative SAP metadata (I_DataExtractionEnabledView).
        - Required fields verified.
        - Source DDL retrievable.
        - Required field coverage sufficient.
        """
        res = cls.classify_source(ddl_source, view_name=view_name)
        details: Dict[str, Any] = {
            "cds_name": view_name,
            "classification": res.classification.value,
            "is_basic": res.is_basic,
            "is_parameterized": res.is_parameterized,
            "extraction_enabled": False,
            "covered_fields": [],
            "missing_fields": [],
            "coverage_pct": 0.0,
        }

        if not res.is_basic or res.is_parameterized:
            return False, f"View '{view_name}' is not a verified non-parameterized BASIC view ({res.evidence})", details

        # Verify extraction in I_DataExtractionEnabledView
        rec = extraction_records.get(view_name.strip().upper())
        if not rec:
            return False, f"View '{view_name}' is not listed in authoritative I_DataExtractionEnabledView.", details

        details["extraction_enabled"] = True
        details["extraction_info"] = rec

        # Verify field coverage
        avail_fields = {
            (f.get("fieldname") or f.get("name") or "").strip().upper()
            for f in field_metadata
        }
        covered = []
        missing = []
        for rf in required_fields:
            rf_clean = rf.strip().upper()
            if rf_clean in avail_fields:
                covered.append(rf)
            else:
                missing.append(rf)

        cov_pct = (len(covered) / len(required_fields) * 100.0) if required_fields else 100.0
        details["covered_fields"] = covered
        details["missing_fields"] = missing
        details["coverage_pct"] = round(cov_pct, 1)

        if cov_pct < 100.0:
            return False, f"View '{view_name}' covers {len(covered)}/{len(required_fields)} required fields ({cov_pct}%). Missing: {missing}", details

        return True, f"Verified BASIC extraction-enabled CDS view '{view_name}' satisfies 100% of required fields.", details

    def is_basic(self, ddl_source: str, view_name: str = "") -> bool:
        """Convenience method returning True if and only if view is verified BASIC and non-parameterized."""
        res = self.classify_source(ddl_source, view_name)
        return res.classification == CDSClassificationType.BASIC and not res.is_parameterized


# Global instance
cds_classifier = CDSClassificationService()
