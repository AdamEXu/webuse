"""Map raw AX nodes to renderable ghost-DOM nodes."""

TEXT_ROLES = {"AXTextField", "AXTextArea", "AXComboBox"}


def classify(n: dict) -> dict | None:
    """Return render info for a raw AX node, or None if it shouldn't render."""
    role, sub = n["role"], n.get("subrole")
    actions = n.get("actions") or []
    v = n.get("value")

    if role in TEXT_ROLES or sub == "AXSearchField":
        return {
            "kind": "text",
            "tag": "textarea" if role == "AXTextArea" else "input",
            "value": v if isinstance(v, str) else ("" if v is None else str(v)),
        }
    if role == "AXCheckBox":
        return {"kind": "press", "tag": "input", "type": "checkbox",
                "checked": v in (1, 2, True, "1")}
    if role == "AXRadioButton":
        return {"kind": "press", "tag": "input", "type": "radio",
                "checked": v in (1, True, "1")}
    if role == "AXLink":
        return {"kind": "press", "tag": "a", "text": n.get("label") or _text(v)}
    if role == "AXStaticText":
        return {"kind": "static", "tag": "span", "text": _text(v) or n.get("label")}
    if role == "AXImage":
        return {"kind": "static", "tag": "span", "text": ""}
    if "AXPress" in actions:
        return {"kind": "press", "tag": "button",
                "text": n.get("label") or _text(v)}
    if isinstance(v, str) and v:
        return {"kind": "static", "tag": "span", "text": v}
    return None


def _text(v):
    return v if isinstance(v, str) else None


def build_nodes(snap: dict) -> list[dict]:
    """Convert a raw snapshot into window-relative render nodes."""
    win = snap["window"]
    out = []
    for n in snap["nodes"]:
        info = classify(n)
        if info is None:
            continue
        x, y = n["x"] - win["x"], n["y"] - win["y"]
        w, h = n["w"], n["h"]
        if w <= 0 or h <= 0:
            continue
        offscreen = x + w < 0 or y + h < 0 or x > win["w"] or y > win["h"]
        node = {
            "id": n["id"],
            "x": round(x, 1), "y": round(y, 1),
            "w": round(w, 1), "h": round(h, 1),
            "label": n.get("label") or "",
            "placeholder": n.get("placeholder") or "",
            "help": n.get("help") or "",
            "disabled": not n.get("enabled", True),
            "offscreen": offscreen,
            **info,
        }
        out.append(node)
    return out
