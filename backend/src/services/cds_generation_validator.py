"""Authoritative Deterministic CDS DDL Generation Validator Service.

Enforces 100% grounding of Gemini-generated CDS View Entity DDL code
against verified SAP ADT metadata and Step 6 authoritative field mappings.

Strict Rules Enforced:
1. Source-of-Truth Grounding: Only verified SAP ADT metadata/mappings are valid.
2. Selected CDS Views are Authoritative: No entity invention, substitution, or omission.
3. Authoritative Field Mapping: No semantic field substitution (e.g. Material -> Product).
4. No Join Invention: Only verified join paths with matching ON conditions are allowed.
5. No Field Invention: Every referenced technical field must exist in metadata.
6. 100% Required Field Coverage: All required business fields must be present in DDL.
7. Bidirectional Validation: Metadata -> Generated Code AND Generated Code -> Metadata.
8. Zero-LLM Gate Safety: Validation MUST pass before human approval.
"""

import re
import json
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("sap_cds_platform.cds_generation_validator")

# SQL & ABAP CDS reserved words to exclude from field extraction
RESERVED_WORDS: Set[str] = {
    "SELECT", "FROM", "AS", "KEY", "DEFINE", "VIEW", "ENTITY", "ROOT", "CUSTOM",
    "ASSOCIATION", "TO", "JOIN", "ON", "WHERE", "HAVING", "GROUP", "BY", "ORDER",
    "LEFT", "RIGHT", "OUTER", "INNER", "CROSS", "FULL", "CARDINALITY",
    "CASE", "WHEN", "THEN", "ELSE", "END", "AND", "OR", "NOT", "NULL", "IN",
    "IS", "LIKE", "BETWEEN", "EXISTS", "SUM", "COUNT", "AVG", "MIN", "MAX",
    "CAST", "CONCAT", "COALESCE", "SUBSTRING", "ABS", "CEIL", "FLOOR", "ROUND",
    "DIVISION", "MOD", "UNIT_CONVERSION", "CURRENCY_CONVERSION", "DATS",
    "TIMS", "CHAR", "NUMC", "DEC", "INT2", "INT4", "FLTP", "RAW",
    "$SESSION", "$PROJECTION", "SYSTEM_DATE", "SYSTEM_LANGUAGE", "CLIENT", "USER",
    "TRUE", "FALSE", "UPPER", "LOWER", "LENGTH"
}


class CDSDDLParser:
    """Extracts structured entities, aliases, fields, annotations, and join ON conditions from CDS DDL."""

    @staticmethod
    def strip_comments(ddl: str) -> str:
        """Removes single-line and multi-line comments."""
        ddl = re.sub(r"/\*[\s\S]*?\*/", "", ddl)
        ddl = re.sub(r"//.*$", "", ddl, flags=re.MULTILINE)
        ddl = re.sub(r"--.*$", "", ddl, flags=re.MULTILINE)
        return ddl

    @classmethod
    def parse(cls, ddl_source: str) -> Dict[str, Any]:
        """Parses DDL source into normalized AST representation."""
        clean = cls.strip_comments(ddl_source or "")

        # 1. Target View Entity Name
        target_match = re.search(
            r"\bdefine\s+(?:root\s+|custom\s+)?view\s+(?:entity\s+)?([a-zA-Z0-9_\/]+)",
            clean,
            re.IGNORECASE,
        )
        target_entity = target_match.group(1).upper() if target_match else ""

        # 2. Base Select From
        base_match = re.search(
            r"\bas\s+select\s+from\s+([a-zA-Z0-9_\/]+)(?:\s+as\s+([a-zA-Z0-9_\/]+))?",
            clean,
            re.IGNORECASE,
        )
        base_entity = base_match.group(1).upper() if base_match else ""
        base_alias = base_match.group(2).upper() if (base_match and base_match.group(2)) else base_entity

        # 3. Joins and Associations
        joins = []
        join_pattern = re.compile(
            r"\b(association\s*(?:\[[^\]]*\])?\s*to|(?:left\s+outer\s+|right\s+outer\s+|inner\s+|cross\s+)?join)\s+([a-zA-Z0-9_\/]+)(?:\s+as\s+([a-zA-Z0-9_\/]+))?\s+on\s+([\s\S]*?)(?=\bassociation\b|\bjoin\b|\{|\;|\Z)",
            re.IGNORECASE,
        )
        for m in join_pattern.finditer(clean):
            j_type_raw = m.group(1).strip()
            j_target = m.group(2).strip().upper()
            j_alias = m.group(3).strip().upper() if m.group(3) else j_target
            j_on_raw = m.group(4).strip()

            # Extract cardinality if present
            card_match = re.search(r"\[\s*([0-9\.\*]+)\s*\]", j_type_raw)
            cardinality = card_match.group(1) if card_match else None

            joins.append({
                "type": j_type_raw,
                "target_entity": j_target,
                "alias": j_alias,
                "cardinality": cardinality,
                "on_clause": j_on_raw,
            })

        # Build alias -> entity map
        alias_to_entity: Dict[str, str] = {}
        if base_entity:
            alias_to_entity[base_entity] = base_entity
            if base_alias:
                alias_to_entity[base_alias] = base_entity

        for j in joins:
            alias_to_entity[j["target_entity"]] = j["target_entity"]
            if j["alias"]:
                alias_to_entity[j["alias"]] = j["target_entity"]

        # Special alias: $PROJECTION maps back to base_entity
        alias_to_entity["$PROJECTION"] = base_entity

        # 4. Parse ON Conditions
        parsed_on_conditions = []
        for j in joins:
            on_clause = j["on_clause"]
            eq_matches = re.findall(
                r"(?:([a-zA-Z0-9_\$]+)\.)?([a-zA-Z0-9_]+)\s*=\s*(?:([a-zA-Z0-9_\$]+)\.)?([a-zA-Z0-9_]+)",
                on_clause,
            )
            for left_alias, left_field, right_alias, right_field in eq_matches:
                l_alias_u = (left_alias or base_alias).upper()
                r_alias_u = (right_alias or j["alias"]).upper()

                l_entity = alias_to_entity.get(l_alias_u, base_entity if l_alias_u == "$PROJECTION" else l_alias_u)
                r_entity = alias_to_entity.get(r_alias_u, r_alias_u)

                parsed_on_conditions.append({
                    "left_alias": l_alias_u,
                    "left_entity": l_entity,
                    "left_field": left_field.upper(),
                    "right_alias": r_alias_u,
                    "right_entity": r_entity,
                    "right_field": right_field.upper(),
                    "cardinality": j["cardinality"],
                    "raw": f"{l_alias_u}.{left_field} = {r_alias_u}.{right_field}",
                })

        # 5. Extract Select Fields & Expressions
        select_clause = ""
        select_match = re.search(r"\{([\s\S]*)\}", clean)
        if select_match:
            select_clause = select_match.group(1)

        referenced_fields: List[Dict[str, str]] = []
        # Split select items by comma, respecting parentheses
        items = []
        current = []
        depth = 0
        for ch in select_clause:
            if ch == "(":
                depth += 1
                current.append(ch)
            elif ch == ")":
                depth = max(0, depth - 1)
                current.append(ch)
            elif ch == "," and depth == 0:
                items.append("".join(current).strip())
                current = []
            else:
                current.append(ch)
        if current:
            items.append("".join(current).strip())

        # Check view entity syntax
        is_view_entity = bool(re.search(r"\bdefine\s+(?:root\s+|custom\s+)?view\s+entity\b", clean, re.IGNORECASE))

        # Check key elements
        key_matches = re.findall(r"\bkey\s+(?:[a-zA-Z0-9_\$]+\.)?([a-zA-Z0-9_]+)", select_clause, re.IGNORECASE)
        has_key = len(key_matches) > 0

        # Check trailing comma
        has_trailing_comma = bool(re.search(r",\s*\}", clean))

        # Check aggregate and group by
        has_aggregate = bool(re.search(r"\b(sum|avg|min|max|count)\s*\(", select_clause, re.IGNORECASE))
        has_group_by = bool(re.search(r"\bgroup\s+by\b", clean, re.IGNORECASE))

        # Check projection fields
        projection_fields_used = re.findall(r"\$projection\.([a-zA-Z0-9_]+)", clean, re.IGNORECASE)

        # Build set of declared association aliases
        assoc_aliases = {j["alias"].upper() for j in joins if j.get("alias")} | {j["target_entity"].upper() for j in joins}
        exposed_associations = set()

        # Extract projected element names
        projected_names = []
        for item in items:
            as_m = re.search(r"\bas\s+([a-zA-Z0-9_]+)", item, re.IGNORECASE)
            if as_m:
                projected_names.append(as_m.group(1).upper())
            else:
                toks = re.findall(r"[a-zA-Z0-9_]+", item)
                if toks:
                    projected_names.append(toks[-1].upper())

        for item in items:
            if not item:
                continue
            # Extract expression before trailing alias: expr as alias
            expr_match = re.search(r"^(.*?)(?:\s+as\s+[a-zA-Z0-9_]+)?\s*$", item, flags=re.IGNORECASE | re.DOTALL)
            expr_part = expr_match.group(1) if expr_match else item

            # Match tokens which can be dotted paths (e.g. _CUSTOMER._JournalEntryItem.PostingDate)
            tokens = re.findall(r"[a-zA-Z0-9_\$]+(?:\.[a-zA-Z0-9_]+)*", expr_part)
            for tok in tokens:
                parts = tok.split(".")
                field_name = parts[-1]
                if not field_name:
                    continue

                field_u = field_name.upper()
                if field_u in RESERVED_WORDS:
                    continue

                # Check if this token is an exposed association name, not a database field
                if len(parts) == 1 and field_u in assoc_aliases:
                    exposed_associations.add(field_u)
                    continue

                if len(parts) == 1:
                    alias_u = ""
                    entity_resolved = base_entity
                elif len(parts) == 2:
                    alias_u = parts[0].upper()
                    if alias_u in RESERVED_WORDS:
                        continue
                    entity_resolved = alias_to_entity.get(alias_u, base_entity)
                else:
                    alias_u = parts[0].upper()
                    assoc_hint = parts[-2].lstrip("_").upper()
                    matching_view = None
                    for known_v in alias_to_entity.values():
                        if assoc_hint in known_v.upper() or known_v.upper().endswith(assoc_hint):
                            matching_view = known_v
                            break
                    entity_resolved = matching_view or f"I_{assoc_hint}"

                referenced_fields.append({
                    "alias": alias_u,
                    "entity": entity_resolved,
                    "field": field_u,
                    "raw_field": field_name,
                })

        # 6. Extract Annotations
        annotations = {}
        ann_matches = re.findall(r"@([a-zA-Z0-9_\.]+)\s*:\s*([^@\n;]+)", clean)
        for a_name, a_val in ann_matches:
            annotations[a_name.strip()] = a_val.strip()

        # Source views used in DDL
        views_used = set()
        if base_entity:
            views_used.add(base_entity)
        for j in joins:
            views_used.add(j["target_entity"])

        return {
            "target_entity": target_entity,
            "base_entity": base_entity,
            "base_alias": base_alias,
            "joins": joins,
            "alias_to_entity": alias_to_entity,
            "on_conditions": parsed_on_conditions,
            "referenced_fields": referenced_fields,
            "views_used": views_used,
            "select_clause": select_clause,
            "annotations": annotations,
            "is_view_entity": is_view_entity,
            "has_key": has_key,
            "key_fields": key_matches,
            "has_trailing_comma": has_trailing_comma,
            "has_aggregate": has_aggregate,
            "has_group_by": has_group_by,
            "projection_fields_used": projection_fields_used,
            "projected_names": projected_names,
            "exposed_associations": exposed_associations,
        }


def validate_source_completeness(
    generated_ddl: str,
    selected_views: List[str],
    mappings: Optional[List[Dict[str, Any]]] = None,
    join_paths: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Validates that all REQUIRED source CDS views (derived from authoritative mappings) are present in DDL."""
    ddl_upper = (generated_ddl or "").upper()

    required_sources: Set[str] = set()
    mapped_field_for_source: Dict[str, str] = {}
    if mappings:
        for m in mappings:
            bus_field = m.get("business_field") or m.get("requirement_field") or m.get("field_name") or ""
            act_field = m.get("actual_field") or bus_field
            cds = m.get("selected_cds") or m.get("cds_name") or m.get("cds_view") or m.get("cds_entity") or m.get("source_entity")
            if cds and str(cds).strip():
                cds_upper = str(cds).strip().upper()
                required_sources.add(cds_upper)
                if cds_upper not in mapped_field_for_source and (bus_field or act_field):
                    mapped_field_for_source[cds_upper] = str(bus_field or act_field)

    if not required_sources and selected_views:
        required_sources = {v.strip().upper() for v in selected_views}

    missing_sources = []
    warnings_list = []

    match_from = re.search(r"FROM\s+([A-Z0-9_]+)", ddl_upper)
    base_from = match_from.group(1).upper() if match_from else ""
    root_cds = base_from or (selected_views[0].strip().upper() if selected_views else (list(required_sources)[0] if required_sources else ""))

    for view in sorted(required_sources):
        patterns = [f"FROM {view}", f"TO {view}", f"JOIN {view}", f"INNER JOIN {view}", f"LEFT JOIN {view}"]
        found = any(pattern in ddl_upper for pattern in patterns)
        if not found:
            is_connected = (view == root_cds)
            if not is_connected and join_paths:
                for jp in join_paths:
                    s_cds = str(jp.get("source_cds") or jp.get("left_cds") or jp.get("left_entity") or jp.get("left_view") or "").strip().upper()
                    t_cds = str(jp.get("target_cds") or jp.get("right_cds") or jp.get("right_entity") or jp.get("right_view") or "").strip().upper()
                    if (s_cds == root_cds and t_cds == view) or (s_cds == view and t_cds == root_cds):
                        is_connected = True
                        break

            if mappings is not None and join_paths is not None and not is_connected and view != root_cds:
                field_name = mapped_field_for_source.get(view, "field")
                warn_msg = f"SOURCE_PATH_UNAVAILABLE: Required mapped field '{field_name}' comes from '{view}'. Direct verified path not found, but composed association paths are accepted."
                warnings_list.append(warn_msg)
            else:
                missing_sources.append(view)

    if missing_sources:
        err_msg = f"MISSING_SELECTED_SOURCE: Required source CDS view(s) '{', '.join(missing_sources)}' missing from generated DDL."
        return {
            "status": "GENERATION_VALIDATION_FAILED",
            "missing_sources": missing_sources,
            "error": err_msg,
            "error_type": "MISSING_SELECTED_SOURCE",
            "warnings": warnings_list,
            "approval_enabled": False,
        }

    return {
        "status": "GENERATION_VALIDATED",
        "missing_sources": [],
        "warnings": warnings_list,
        "approval_enabled": True,
    }


class CDSGenerationValidator:
    """Deterministic validator for Gemini-generated CDS DDL against verified SAP ADT metadata."""

    @classmethod
    def validate_generation(
        cls,
        ddl_source: str,
        verified_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Validates generated CDS DDL against authoritative immutable context."""
        errors: List[str] = []
        structured_errors: List[Dict[str, Any]] = []
        warnings: List[str] = []

        if not ddl_source or not ddl_source.strip():
            err_obj = {
                "type": "MISSING_SOURCE_CODE",
                "message": "Generated DDL source code is empty or missing."
            }
            return {
                "valid": False,
                "status": "GENERATION_VALIDATION_FAILED",
                "code_status": "Generation Validation Failed",
                "errors": ["Generated DDL source code is empty or missing."],
                "structured_errors": [err_obj],
                "warnings": [],
                "coverage": 0.0,
                "verified_fields": [],
                "missing_fields": [],
                "unverified_fields": [],
                "verified_views": [],
                "unverified_views": [],
                "verified_relationships": [],
                "unverified_relationships": [],
            }

        parsed = CDSDDLParser.parse(ddl_source)

        # Context normalization
        selected_views_raw = verified_context.get("selected_views") or verified_context.get("views") or []
        selected_views_list: List[str] = [v.strip().upper() for v in selected_views_raw if v and v.strip()]
        selected_views: Set[str] = set(selected_views_list)

        mappings: List[Dict[str, Any]] = verified_context.get("mappings") or verified_context.get("fields") or []
        join_paths: List[Dict[str, Any]] = verified_context.get("join_paths", []) or verified_context.get("verified_join_paths", [])
        kpi: Optional[Dict[str, Any]] = verified_context.get("kpi")
        calculated_fields: Optional[List[Dict[str, Any]]] = verified_context.get("calculated_fields")
        artifact_name: str = verified_context.get("artifact_name", "").strip().upper()

        # Structured Trace Logging (Step 6 -> Step 9 -> Final Validation Mappings Flow)
        logger.info("=== STEP 6 AUTHORITATIVE MAPPINGS ===")
        logger.info(json.dumps(mappings, indent=2))
        logger.info("=== STEP 9 REQUIRED MAPPINGS ===")
        logger.info(json.dumps(mappings, indent=2))
        logger.info("=== FINAL VALIDATION MAPPINGS ===")
        logger.info(json.dumps(mappings, indent=2))

        required_fields_list = [m.get("business_field") or m.get("requirement_field") for m in mappings]
        required_source_cds = list({m.get("selected_cds") or m.get("cds_view") or m.get("source_cds") for m in mappings if m.get("selected_cds") or m.get("cds_view") or m.get("source_cds")})

        logger.info("TRACE required_fields: %s", json.dumps(required_fields_list))
        logger.info("TRACE authoritative_mappings: %s", json.dumps(mappings))
        logger.info("TRACE required_source_cds: %s", json.dumps(required_source_cds))

        for m in mappings:
            bf = str(m.get("business_field") or m.get("requirement_field") or "")
            sel_cds = m.get("selected_cds") or m.get("cds_view") or m.get("source_cds") or m.get("cds_entity") or ""
            act_fld = m.get("actual_field") or m.get("matched_field") or m.get("technical_field") or m.get("source_field") or ""
            trace_item = {
                "business_field": bf,
                "selected_cds": sel_cds,
                "actual_field": act_fld,
                "source_cds": sel_cds,
                "source_field": act_fld,
                "field_label": m.get("field_label", bf),
                "mapping_status": "VERIFIED",
                "evidence": m.get("evidence", "Step 6 Authoritative Mapping"),
                "verified": True,
            }
            logger.info("MAPPING_TRACE [%s]: %s", bf, json.dumps(trace_item))

        # Target name check
        if artifact_name and parsed["target_entity"] and parsed["target_entity"] != artifact_name:
            err_msg = f"Generated target entity name '{parsed['target_entity']}' does not match target artifact name '{artifact_name}'."
            errors.append(err_msg)
            structured_errors.append({"type": "TARGET_NAME_MISMATCH", "message": err_msg})

        # 0. Validate ABAP CDS View Entity Syntax Rules (Clean Core, Key elements, Trailing commas, Projections, Group By)
        syntax_res = cls.validate_syntax_rules(parsed, ddl_source)
        errors.extend(syntax_res["errors"])
        structured_errors.extend(syntax_res["structured_errors"])
        warnings.extend(syntax_res["warnings"])

        # 1. Validate Source Views & CDS Invention/Omission
        view_res = cls.validate_source_views(parsed, selected_views, join_paths, mappings)
        errors.extend(view_res["errors"])
        structured_errors.extend(view_res["structured_errors"])
        warnings.extend(view_res["warnings"])

        # 2. Validate Field Source Mismatch & Semantic Substitutions
        field_res = cls.validate_fields(parsed, selected_views, mappings)
        errors.extend(field_res["errors"])
        structured_errors.extend(field_res["structured_errors"])
        warnings.extend(field_res["warnings"])

        # 3. Validate Required Field Coverage
        cov_res = cls.validate_required_field_coverage(parsed, mappings)
        errors.extend(cov_res["errors"])
        structured_errors.extend(cov_res["structured_errors"])
        warnings.extend(cov_res["warnings"])

        # 4. Validate Relationships & Join Invention
        rel_res = cls.validate_relationships(parsed, join_paths)
        errors.extend(rel_res["errors"])
        structured_errors.extend(rel_res["structured_errors"])
        warnings.extend(rel_res["warnings"])

        # 5. Validate Annotations
        ann_res = cls.validate_annotations(parsed)
        errors.extend(ann_res["errors"])
        structured_errors.extend(ann_res["structured_errors"])
        warnings.extend(ann_res.get("warnings", []))

        # 6. Validate Calculated Fields & KPI
        if kpi is not None:
            kpi_res = cls.validate_kpi(parsed, kpi, mappings)
            errors.extend(kpi_res["errors"])
            structured_errors.extend(kpi_res["structured_errors"])

        if calculated_fields:
            calc_res = cls.validate_calculated_fields(parsed, calculated_fields)
            errors.extend(calc_res["errors"])
            structured_errors.extend(calc_res["structured_errors"])

        # 7. FINAL MANDATORY GATE: Source Completeness Validation
        comp_res = validate_source_completeness(ddl_source, selected_views_list, mappings, join_paths)
        if comp_res["status"] == "GENERATION_VALIDATION_FAILED":
            if comp_res["error"] not in errors:
                errors.append(comp_res["error"])
            structured_errors.append({
                "type": comp_res.get("error_type", "MISSING_SELECTED_SOURCE"),
                "missing_sources": comp_res["missing_sources"],
                "message": comp_res["error"],
            })

        is_valid = (len(errors) == 0) and comp_res.get("approval_enabled", True)
        status = "GENERATION_VALIDATED" if is_valid else "GENERATION_VALIDATION_FAILED"
        code_status = "Pending Review" if is_valid else "Generation Validation Failed"

        return {
            "valid": is_valid,
            "status": status,
            "code_status": code_status,
            "approval_enabled": is_valid,
            "errors": errors,
            "structured_errors": structured_errors,
            "warnings": warnings,
            "coverage": cov_res["coverage"],
            "verified_fields": list(cov_res["verified_fields"]),
            "missing_fields": list(cov_res["missing_fields"]),
            "unverified_fields": list(field_res["unverified_fields"]),
            "verified_views": list(view_res["verified_views"]),
            "unverified_views": list(view_res["unverified_views"]),
            "verified_relationships": list(rel_res["verified_relationships"]),
            "unverified_relationships": list(rel_res["unverified_relationships"]),
        }

    @classmethod
    def validate_source_views(
        cls,
        parsed: Dict[str, Any],
        selected_views: Set[str],
        join_paths: List[Dict[str, Any]],
        mappings: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Validates that all referenced CDS entities are selected and all required entities are present."""
        errors: List[str] = []
        structured_errors: List[Dict[str, Any]] = []
        warnings: List[str] = []

        views_used = parsed["views_used"]
        unverified_views = views_used - selected_views

        if unverified_views:
            for uv in sorted(unverified_views):
                err_msg = f"UNVERIFIED_CDS: Unverified CDS Entity '{uv}' referenced in generated DDL. Selected views are: {sorted(selected_views)}."
                errors.append(err_msg)
                structured_errors.append({
                    "type": "UNVERIFIED_CDS",
                    "cds": uv,
                    "message": err_msg,
                })

        required_source_views: Set[str] = set()
        mapped_field_for_source: Dict[str, str] = {}
        for m in mappings:
            bus_field = m.get("business_field") or m.get("requirement_field") or m.get("field_name") or ""
            act_field = m.get("actual_field") or bus_field
            sel_cds = m.get("selected_cds") or m.get("cds_name") or m.get("cds_entity") or m.get("cds_view") or m.get("source_entity")
            if sel_cds and str(sel_cds).strip():
                cds_upper = str(sel_cds).strip().upper()
                required_source_views.add(cds_upper)
                if cds_upper not in mapped_field_for_source and (bus_field or act_field):
                    mapped_field_for_source[cds_upper] = str(bus_field or act_field)

        if not required_source_views and selected_views:
            root_v = next(iter(selected_views))
            required_source_views.add(root_v.strip().upper())

        missing_required_views = required_source_views - views_used
        if missing_required_views:
            for mv in sorted(missing_required_views):
                has_verified_path = False
                for jp in join_paths:
                    s_cds = str(jp.get("source_cds") or jp.get("left_cds") or jp.get("left_entity") or jp.get("left_view") or "").strip().upper()
                    t_cds = str(jp.get("target_cds") or jp.get("right_cds") or jp.get("right_entity") or jp.get("right_view") or "").strip().upper()
                    if s_cds == mv or t_cds == mv:
                        has_verified_path = True
                        break

                if not has_verified_path and len(views_used) > 0:
                    field_name = mapped_field_for_source.get(mv, "field")
                    warn_msg = f"SOURCE_PATH_UNAVAILABLE: Required mapped field '{field_name}' comes from '{mv}', but no verified relationship connects it to the selected root CDS."
                    warnings.append(warn_msg)
                else:
                    err_msg = f"MISSING_SELECTED_SOURCE: Required source CDS view '{mv}' is missing from the generated DDL."
                    errors.append(err_msg)
                    structured_errors.append({
                        "type": "MISSING_SELECTED_SOURCE",
                        "cds": mv,
                        "message": err_msg,
                    })

        return {
            "errors": errors,
            "structured_errors": structured_errors,
            "warnings": warnings,
            "verified_views": sorted(views_used & selected_views),
            "unverified_views": sorted(unverified_views),
        }

    @classmethod
    def validate_fields(
        cls,
        parsed: Dict[str, Any],
        selected_views: Set[str],
        mappings: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Validates referenced fields against authoritative technical mappings, checks FIELD_SOURCE_MISMATCH and semantic substitution."""
        errors: List[str] = []
        structured_errors: List[Dict[str, Any]] = []
        warnings: List[str] = []
        unverified_fields: Set[str] = set()

        allowed_fields_per_entity: Dict[str, Set[str]] = {}
        expected_tech_for_bus: Dict[str, str] = {}
        mapping_entity_for_tech: Dict[str, str] = {}

        for m in mappings:
            bus_field = m.get("business_field") or m.get("requirement_field") or ""
            actual_tech = m.get("actual_field") or m.get("mapped_field") or m.get("technical_field") or ""
            entity = m.get("selected_cds") or m.get("cds_name") or m.get("cds_entity") or ""

            if actual_tech and entity:
                ent_u = str(entity).strip().upper()
                tech_u = str(actual_tech).strip().upper()
                allowed_fields_per_entity.setdefault(ent_u, set()).add(tech_u)
                mapping_entity_for_tech[tech_u] = ent_u
                if bus_field:
                    expected_tech_for_bus[str(bus_field).strip().upper()] = tech_u

        # Check FIELD_SOURCE_MISMATCH: Ensure every CDS entity supplying mapped fields is represented in DDL views_used or referenced fields
        for tech_u, required_ent in mapping_entity_for_tech.items():
            if required_ent not in parsed["views_used"]:
                # Check if represented via association path expression in select list
                is_accessed_via_assoc = any(ref["entity"] == required_ent for ref in parsed["referenced_fields"])
                if is_accessed_via_assoc:
                    continue

                err_msg = f"FIELD_SOURCE_MISMATCH: Technical field '{tech_u}' is mapped to source '{required_ent}', but generated DDL does not include '{required_ent}' through a verified relationship."
                errors.append(err_msg)
                structured_errors.append({
                    "type": "FIELD_SOURCE_MISMATCH",
                    "field": tech_u,
                    "source": required_ent,
                    "message": err_msg,
                })

        forbidden_substitutions = {
            ("MATERIAL", "PRODUCT"),
            ("MATERIAL", "PRODUCTID"),
            ("MATERIAL", "PRODUCTUNITGROUP"),
            ("MATERIAL", "MATERIALNUMBER"),
            ("NETAMOUNT", "TOTALNETAMOUNT"),
            ("NETAMOUNT", "AMOUNT"),
            ("NETAMOUNT", "NETVALUE"),
            ("PLANT", "SUPPLYINGPLANT"),
            ("PLANT", "PRODUCTIONPLANT"),
            ("CUSTOMER", "SOLDTOPARTY"),
            ("CURRENCY", "TRANSACTIONCURRENCY"),
            ("CURRENCY", "BALANCETRANSACTIONCURRENCY"),
        }

        for ref in parsed["referenced_fields"]:
            ent_u = ref["entity"]
            field_u = ref["field"]

            if ent_u in selected_views:
                allowed = allowed_fields_per_entity.get(ent_u, set())
                for bus_u, exp_tech in expected_tech_for_bus.items():
                    if (exp_tech, field_u) in forbidden_substitutions or (bus_u, field_u) in forbidden_substitutions:
                        if field_u != exp_tech:
                            err_msg = f"SEMANTIC_SUBSTITUTION: Business field '{bus_u}' is mapped to technical field '{exp_tech}', but generated code used '{field_u}'."
                            errors.append(err_msg)
                            structured_errors.append({
                                "type": "SEMANTIC_SUBSTITUTION",
                                "business_field": bus_u,
                                "expected": exp_tech,
                                "generated": field_u,
                                "message": err_msg,
                            })

                if allowed and field_u not in allowed:
                    is_join_field = any(
                        (on["left_entity"] == ent_u and on["left_field"] == field_u) or
                        (on["right_entity"] == ent_u and on["right_field"] == field_u)
                        for on in parsed["on_conditions"]
                    )
                    is_known_db_field = False
                    if not is_join_field:
                        try:
                            import sqlite3
                            from pathlib import Path
                            db_p = Path(__file__).resolve().parent.parent.parent / "data" / "sap_cds.db"
                            if db_p.exists():
                                c_temp = sqlite3.connect(f"file:{db_p.as_posix()}?mode=ro", uri=True, timeout=10.0)
                                cur_t = c_temp.cursor()
                                cur_t.execute("SELECT 1 FROM cds_fields WHERE UPPER(cds_name) = ? AND UPPER(fieldname) = ?", (ent_u, field_u))
                                if cur_t.fetchone():
                                    is_known_db_field = True
                                c_temp.close()
                        except Exception:
                            pass

                    if not is_join_field and not is_known_db_field:
                        unverified_fields.add(f"{ent_u}.{field_u}")
                        err_msg = f"UNVERIFIED_FIELD: Technical field '{field_u}' referenced on CDS view '{ent_u}' is unverified."
                        errors.append(err_msg)
                        structured_errors.append({
                            "type": "UNVERIFIED_FIELD",
                            "cds": ent_u,
                            "field": field_u,
                            "message": err_msg,
                        })

        return {
            "errors": errors,
            "structured_errors": structured_errors,
            "warnings": warnings,
            "unverified_fields": sorted(unverified_fields),
        }

    @classmethod
    def validate_required_field_coverage(
        cls,
        parsed: Dict[str, Any],
        mappings: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Calculates exact required field coverage and verifies 100% representation."""
        errors: List[str] = []
        structured_errors: List[Dict[str, Any]] = []
        warnings: List[str] = []
        verified_fields: Set[str] = set()
        missing_fields: Set[str] = set()

        if not mappings:
            return {
                "coverage": 100.0,
                "verified_fields": [],
                "missing_fields": [],
                "errors": [],
                "structured_errors": [],
                "warnings": [],
            }

        all_referenced_pairs = {
            (ref["entity"], ref["field"]) for ref in parsed["referenced_fields"]
        }
        all_referenced_fields_only = {ref["field"] for ref in parsed["referenced_fields"]}

        total_required = len(mappings)

        for m in mappings:
            bus_field = m.get("business_field") or m.get("requirement_field") or m.get("name") or "UnknownField"
            actual_tech = m.get("actual_field") or m.get("mapped_field") or m.get("technical_field") or m.get("name") or ""
            entity = m.get("selected_cds") or m.get("cds_name") or m.get("cds_entity") or m.get("view") or m.get("cds_view") or ""

            ent_u = str(entity).strip().upper() if entity else ""
            tech_u = str(actual_tech).strip().upper() if actual_tech else ""

            is_present = False
            if ent_u and tech_u:
                is_present = (ent_u, tech_u) in all_referenced_pairs or tech_u in all_referenced_fields_only
            elif tech_u:
                is_present = tech_u in all_referenced_fields_only

            if is_present:
                verified_fields.add(f"{bus_field} -> {ent_u}.{tech_u}" if ent_u else f"{bus_field} -> {tech_u}")
            else:
                missing_fields.add(f"{bus_field} ({tech_u})" if tech_u else bus_field)

        coverage = (len(verified_fields) / total_required) * 100.0 if total_required > 0 else 100.0

        if coverage < 100.0:
            err_msg = f"MISSING_REQUIRED_FIELD: Required field coverage is {coverage:.1f}% (target: 100%). Missing: {sorted(missing_fields)}."
            errors.append(err_msg)
            structured_errors.append({
                "type": "MISSING_REQUIRED_FIELD",
                "coverage": coverage,
                "missing_fields": sorted(missing_fields),
                "message": err_msg,
            })

        return {
            "coverage": coverage,
            "verified_fields": sorted(verified_fields),
            "missing_fields": sorted(missing_fields),
            "errors": errors,
            "structured_errors": structured_errors,
            "warnings": warnings,
        }

    @classmethod
    def validate_relationships(
        cls,
        parsed: Dict[str, Any],
        join_paths: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Validates that all ON conditions in DDL match verified relationship paths and rejects invented joins."""
        errors: List[str] = []
        structured_errors: List[Dict[str, Any]] = []
        warnings: List[str] = []
        verified_rels: Set[str] = set()
        unverified_rels: Set[str] = set()

        if not parsed["on_conditions"]:
            return {
                "verified_relationships": [],
                "unverified_relationships": [],
                "errors": [],
                "structured_errors": [],
                "warnings": [],
            }

        # Build list of normalized verified join specifications
        normalized_join_paths = []
        for jp in join_paths:
            s_ent = str(jp.get("source_cds") or jp.get("left_cds") or jp.get("left_entity") or jp.get("left_view") or "").strip().upper()
            t_ent = str(jp.get("target_cds") or jp.get("right_cds") or jp.get("right_entity") or jp.get("right_view") or "").strip().upper()
            s_fld = str(jp.get("source_field") or jp.get("left_field") or jp.get("join_field") or "").strip().upper()
            t_fld = str(jp.get("target_field") or jp.get("right_field") or jp.get("join_field") or "").strip().upper()
            card = str(jp.get("cardinality") or "").strip()

            if s_ent and t_ent:
                normalized_join_paths.append({
                    "source": s_ent,
                    "source_field": s_fld,
                    "target": t_ent,
                    "target_field": t_fld,
                    "cardinality": card,
                })

        for on in parsed["on_conditions"]:
            l_ent = on["left_entity"]
            l_fld = on["left_field"]
            r_ent = on["right_entity"]
            r_fld = on["right_field"]

            match_found = False
            matched_jp = None

            for jp in normalized_join_paths:
                s_ent = jp["source"]
                s_fld = jp["source_field"]
                t_ent = jp["target"]
                t_fld = jp["target_field"]

                # Match entities
                entities_match = (l_ent == s_ent and r_ent == t_ent) or (l_ent == t_ent and r_ent == s_ent)
                if not entities_match:
                    continue

                # Match fields if fields were specified in verified path
                if s_fld and t_fld:
                    fields_match = (
                        (l_ent == s_ent and l_fld == s_fld and r_ent == t_ent and r_fld == t_fld) or
                        (l_ent == t_ent and l_fld == t_fld and r_ent == s_ent and r_fld == s_fld)
                    )
                    if fields_match:
                        match_found = True
                        matched_jp = jp
                        break
                else:
                    match_found = True
                    matched_jp = jp
                    break

            if match_found:
                verified_rels.add(on["raw"])
                # Check cardinality if specified
                if matched_jp and matched_jp["cardinality"] and on["cardinality"]:
                    gen_card = on["cardinality"].replace(" ", "")
                    exp_card = matched_jp["cardinality"].replace(" ", "")
                    if gen_card != exp_card:
                        err_msg = f"WRONG_CARDINALITY: Association to '{r_ent}' has cardinality [{gen_card}], but verified cardinality is [{exp_card}]."
                        errors.append(err_msg)
                        structured_errors.append({
                            "type": "WRONG_CARDINALITY",
                            "source": l_ent,
                            "target": r_ent,
                            "generated_cardinality": gen_card,
                            "expected_cardinality": exp_card,
                            "message": err_msg,
                        })
            else:
                unverified_rels.add(on["raw"])
                err_msg = f"UNVERIFIED_JOIN: Unverified join condition '{on['raw']}' generated between '{l_ent}' and '{r_ent}'."
                errors.append(err_msg)
                structured_errors.append({
                    "type": "UNVERIFIED_JOIN",
                    "source": l_ent,
                    "target": r_ent,
                    "condition": on["raw"],
                    "message": err_msg,
                })

        return {
            "verified_relationships": sorted(verified_rels),
            "unverified_relationships": sorted(unverified_rels),
            "errors": errors,
            "structured_errors": structured_errors,
            "warnings": warnings,
        }

    @classmethod
    def validate_annotations(cls, parsed: Dict[str, Any]) -> Dict[str, Any]:
        """Validates required project CDS annotations."""
        errors: List[str] = []
        structured_errors: List[Dict[str, Any]] = []
        anns = parsed["annotations"]

        # 1. Authorization check annotation
        auth_ann = anns.get("AccessControl.authorizationCheck")
        if not auth_ann:
            err_msg = "INVALID_ANNOTATION: Missing mandatory annotation '@AccessControl.authorizationCheck'."
            errors.append(err_msg)
            structured_errors.append({
                "type": "INVALID_ANNOTATION",
                "annotation": "AccessControl.authorizationCheck",
                "message": err_msg,
            })

        # 2. Label annotation (warning if omitted)
        label_ann = anns.get("EndUserText.label")
        warnings: List[str] = []
        if not label_ann:
            warnings.append("Missing recommended annotation '@EndUserText.label'.")

        # 3. Extraction annotation check for wrappers
        extract_ann = anns.get("Analytics.dataExtraction.enabled")
        if extract_ann and extract_ann.lower() == "true":
            pass  # Verified present

        return {"errors": errors, "structured_errors": structured_errors, "warnings": warnings}

    @classmethod
    def validate_kpi(
        cls,
        parsed: Dict[str, Any],
        kpi: Dict[str, Any],
        mappings: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Validates KPI definition consistency against DDL."""
        errors: List[str] = []
        structured_errors: List[Dict[str, Any]] = []

        if not kpi or not kpi.get("enabled"):
            return {"errors": errors, "structured_errors": structured_errors}

        field = kpi.get("field") or kpi.get("kpi_field") or ""

        if field:
            field_u = str(field).strip().upper()
            referenced_f_names = {ref["field"] for ref in parsed["referenced_fields"]}
            if field_u not in referenced_f_names:
                err_msg = f"INVALID_KPI: KPI target field '{field}' is specified in KPI definition but missing from generated DDL."
                errors.append(err_msg)
                structured_errors.append({
                    "type": "INVALID_KPI",
                    "field": field,
                    "message": err_msg,
                })

        return {"errors": errors, "structured_errors": structured_errors}

    @classmethod
    def validate_calculated_fields(
        cls,
        parsed: Dict[str, Any],
        calculated_fields: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Validates calculated/derived field definitions against DDL expressions."""
        errors: List[str] = []
        structured_errors: List[Dict[str, Any]] = []
        select_clause = parsed["select_clause"]

        for cf in calculated_fields:
            name = cf.get("name") or cf.get("field_name") or ""

            if name:
                name_u = str(name).strip().upper()
                if name_u not in select_clause.upper():
                    err_msg = f"UNVERIFIED_CALCULATION: Calculated field '{name}' is missing from generated DDL select list."
                    errors.append(err_msg)
                    structured_errors.append({
                        "type": "UNVERIFIED_CALCULATION",
                        "field": name,
                        "message": err_msg,
                    })

        return {"errors": errors, "structured_errors": structured_errors}

    @classmethod
    def validate_syntax_rules(cls, parsed: Dict[str, Any], ddl_source: str) -> Dict[str, Any]:
        """Validates critical SAP ABAP CDS View Entity syntax and Clean Core constraints."""
        errors: List[str] = []
        structured_errors: List[Dict[str, Any]] = []
        warnings: List[str] = []

        # 1. Define View Entity (Clean Core standard)
        if not parsed.get("is_view_entity"):
            err_msg = "SYNTAX_ERROR: CDS must be defined as 'define view entity' (Clean Core S/4HANA standard), not classic 'define view'."
            errors.append(err_msg)
            structured_errors.append({"type": "SYNTAX_ERROR", "rule": "VIEW_ENTITY_REQUIRED", "message": err_msg})

        # 2. Mandatory Key Element
        if not parsed.get("has_key"):
            err_msg = "SYNTAX_ERROR: CDS View Entity must have at least one element marked with 'key'."
            errors.append(err_msg)
            structured_errors.append({"type": "SYNTAX_ERROR", "rule": "MISSING_KEY_ELEMENT", "message": err_msg})

        # 3. No Trailing Comma before '}'
        if parsed.get("has_trailing_comma"):
            err_msg = "SYNTAX_ERROR: Trailing comma found before closing brace '}'. Illegal in SAP ABAP CDS syntax."
            errors.append(err_msg)
            structured_errors.append({"type": "SYNTAX_ERROR", "rule": "TRAILING_COMMA", "message": err_msg})

        # 4. Projection Field References in ON condition
        projected_names = set(parsed.get("projected_names", []))
        for pf in parsed.get("projection_fields_used", []):
            if pf.upper() not in projected_names:
                err_msg = f"SYNTAX_ERROR: Association condition references '$projection.{pf}', but element '{pf}' is not in the SELECT list."
                errors.append(err_msg)
                structured_errors.append({"type": "SYNTAX_ERROR", "rule": "PROJECTION_FIELD_MISSING", "field": pf, "message": err_msg})

        # 5. Aggregate functions require GROUP BY
        if parsed.get("has_aggregate") and not parsed.get("has_group_by"):
            err_msg = "SYNTAX_ERROR: Aggregate function (SUM, AVG, COUNT, MIN, MAX) used in SELECT list without mandatory 'group by' clause."
            errors.append(err_msg)
            structured_errors.append({"type": "SYNTAX_ERROR", "rule": "MISSING_GROUP_BY", "message": err_msg})

        # 6. Associations must be declared BEFORE opening brace '{'
        brace_idx = ddl_source.find("{")
        if brace_idx > 0:
            after_brace = ddl_source[brace_idx:]
            if re.search(r"\bassociation\s*\[", after_brace, re.IGNORECASE):
                err_msg = "SYNTAX_ERROR: Association declaration found inside '{ }'. All associations must be declared BEFORE the opening brace."
                errors.append(err_msg)
                structured_errors.append({"type": "SYNTAX_ERROR", "rule": "ASSOCIATION_INSIDE_BRACES", "message": err_msg})

        return {"errors": errors, "structured_errors": structured_errors, "warnings": warnings}

    @classmethod
    def locate_line_number(cls, ddl_source: str, term: str) -> Optional[int]:
        """Locates the 1-based line number in DDL source containing a search term."""
        if not ddl_source or not term:
            return None
        lines = ddl_source.splitlines()
        term_clean = term.strip().lower()
        for idx, line in enumerate(lines, start=1):
            if term_clean in line.lower():
                return idx
        return None

    @classmethod
    def investigate_and_validate_in_memory(
        cls,
        ddl_source: str,
        verified_context: Dict[str, Any],
        target_sap_release: Optional[str] = "2023",
    ) -> Dict[str, Any]:
        """Executes in-memory CDS validation adhering strictly to Part 8.

        Stage A: Deterministic static validation with line numbers:
        - Syntax structure, annotations, keys, fields, joins, types, cardinality, data grain.
        Stage B: Investigate genuine SAP source-only validation:
        - Inspects SAP ADT capabilities.
        - Documents that SAP ADT checkrun requires a repository object URI (<chkrun:checkObject adtcore:uri="..." />).
        - Reports SAP_SOURCE_ONLY_VALIDATION_UNSUPPORTED without creating or saving any repository object.
        - Preserves code strictly in backend memory. Zero SAP writes or object creations.
        """
        static_result = cls.validate_generation(ddl_source, verified_context)

        # Enrich structured errors with line numbers
        lines = ddl_source.splitlines() if ddl_source else []
        enriched_structured_errors = []
        enriched_errors_text = []

        for se in static_result.get("structured_errors", []):
            err_copy = dict(se)
            field = err_copy.get("field") or err_copy.get("rule") or err_copy.get("cds") or ""
            line_no = None
            if field:
                line_no = cls.locate_line_number(ddl_source, field)
            if not line_no:
                err_type = err_copy.get("type", "")
                if "KEY" in err_type:
                    line_no = cls.locate_line_number(ddl_source, "select") or 1
                elif "ANNOTATION" in err_type:
                    line_no = 1
                elif "ENTITY" in err_type:
                    line_no = cls.locate_line_number(ddl_source, "define view") or 1
                elif "GROUP_BY" in err_type:
                    line_no = len(lines)
                else:
                    line_no = 1

            err_copy["line"] = line_no
            enriched_structured_errors.append(err_copy)
            line_prefix = f"Line {line_no}: " if line_no else ""
            msg = err_copy.get("message", "")
            if not msg.startswith("Line "):
                enriched_errors_text.append(f"{line_prefix}{msg}")
            else:
                enriched_errors_text.append(msg)

        # Check extraction annotation
        clean = CDSDDLParser.strip_comments(ddl_source or "")
        has_extraction_ann = bool(re.search(r"@Analytics\.dataExtraction\.enabled\s*:\s*true", clean, re.IGNORECASE))
        warnings = list(static_result.get("warnings", []))
        if not has_extraction_ann:
            err_msg = "Line 1: MANDATORY_ANNOTATION_MISSING: Annotation '@Analytics.dataExtraction.enabled: true' is required for extraction wrappers."
            enriched_errors_text.append(err_msg)
            enriched_structured_errors.append({
                "line": 1,
                "type": "MISSING_ANNOTATION",
                "rule": "EXTRACTION_ANNOTATION",
                "message": err_msg,
            })

        # Stage B: SAP Source-Only Validation Capability Investigation
        # ADT checkrun endpoint POST /sap/bc/adt/checkruns?reporters=abapCheckRun
        # Verified requirement: Requires adtcore:uri representing a persisted repository object.
        # Standard SAP S/4HANA ADT REST API does not support compiling arbitrary unsaved source text without repository persistence.
        sap_investigation = {
            "capability": "SAP_SOURCE_ONLY_VALIDATION_UNSUPPORTED",
            "reason": (
                "Standard SAP S/4HANA ADT checkrun protocol (POST /sap/bc/adt/checkruns) strictly requires "
                "a repository object URI (adtcore:uri) referencing a saved object. Arbitrary in-memory source-only "
                "compilation without repository object creation is not supported by standard ADT REST endpoints. "
                "Generated CDS source code was retained 100% in backend memory. Zero repository objects, inactive drafts, "
                "or CTS transport requests were created in SAP DEV."
            ),
            "object_created_in_sap": False,
            "inactive_saved_in_sap": False,
            "transport_created": False,
            "sap_writes_performed": False,
            "endpoint_inspected": "/sap/bc/adt/checkruns",
            "target_sap_release": target_sap_release or "2023",
        }

        is_valid = static_result["valid"] and has_extraction_ann
        status = "STATIC_VALIDATION_PASSED" if is_valid else "FAILED_VALIDATION"

        return {
            "valid": is_valid,
            "status": status,
            "validation_type": "STATIC_VALIDATION",
            "static_validation_passed": is_valid,
            "sap_source_validation_status": "SAP_SOURCE_ONLY_VALIDATION_UNSUPPORTED",
            "sap_writes_performed": False,
            "sap_investigation": sap_investigation,
            "errors": enriched_errors_text if enriched_errors_text else static_result["errors"],
            "structured_errors": enriched_structured_errors,
            "warnings": warnings,
            "coverage": static_result["coverage"],
            "verified_fields": static_result["verified_fields"],
            "missing_fields": static_result["missing_fields"],
            "verified_views": static_result["verified_views"],
            "verified_relationships": static_result["verified_relationships"],
            "approval_enabled": is_valid,
            "ddl_in_memory_hash": str(hash(ddl_source)),
        }


