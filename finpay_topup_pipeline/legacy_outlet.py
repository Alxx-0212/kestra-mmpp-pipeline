import re


LEGACY_OUTLETS_BY_CLUSTER = {
    "411311": frozenset({"GUNUNG MAS", "KATINGAN", "PALANGKARAYA"}),
    "421306": frozenset({"JAILOLO", "MOROTAI", "SUBAIM-BULI", "TOBELO-KAO"}),
    "421307": frozenset({"BACAN-OBI", "SOFIFI-TIDORE", "WEDA"}),
    "421315": frozenset({"AMPANA", "BALUT", "LUWUK"}),
    "421318": frozenset({"BAHODOPI", "BETELEME", "BUNGKU", "POSO"}),
    "421320": frozenset({"SANANA-TALIABU", "TERNATE"}),
}

LEGACY_OUTLET_ALIASES = {
    ("421307", "BCAN-OBI"): "BACAN-OBI",
    ("421320", "TALIABU-SANANA"): "SANANA-TALIABU",
}

_ANNOTATED_MARKERS = (
    "KURANG",
    "LEBIH",
    "BELUM MASUK",
)


def normalize_outlet(raw, cluster_id):
    """Return ``(canonical_label, confidence)`` for a trusted legacy label."""
    value = re.sub(r"\s+", " ", str(raw or "").strip().upper())
    if not value:
        return None, None
    alias = LEGACY_OUTLET_ALIASES.get((str(cluster_id), value))
    if alias:
        return alias, "derived"
    allowed = LEGACY_OUTLETS_BY_CLUSTER.get(str(cluster_id), frozenset())
    if value in allowed:
        return value, "exact"
    for outlet in sorted(allowed, key=len, reverse=True):
        if value.startswith(outlet + " ") and any(marker in value for marker in _ANNOTATED_MARKERS):
            return outlet, "derived"
    return None, "ambiguous"


def row_outlet_candidates(row, remarks_index, cluster_id, outlet_index=None):
    """Extract explicit/trailing outlet candidates without treating remarks as outlets."""
    values = []
    if outlet_index is not None and outlet_index < len(row):
        values.append(row[outlet_index])
    if remarks_index is not None:
        values.extend(row[remarks_index + 1:])
    candidates = []
    for value in values:
        label, confidence = normalize_outlet(value, cluster_id)
        if label:
            candidates.append((label, confidence, str(value).strip()))
    return candidates


def outlet_code(cluster_id, label):
    slug = re.sub(r"[^A-Z0-9]+", "_", label.upper()).strip("_")
    return f"LEGACY_{cluster_id}_{slug}"
