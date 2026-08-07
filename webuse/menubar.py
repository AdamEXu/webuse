"""Read an app's native menu bar over the Accessibility API so it can be
rendered as HTML and driven by pressing menu items — no event injection.

The app's AXMenuBar holds AXMenuBarItem children (Apple, File, Edit, …); each
opens an AXMenu of AXMenuItem rows, and a row with a submenu carries its own
nested AXMenu. Leaf rows are registered in ax's id registry so a normal
`/api/action` press (AXPress) triggers them — the same way ghost controls work.
Only the user's final selection is sent to the app; the menu itself is rendered
client-side.
"""

from ApplicationServices import AXUIElementCreateApplication

from . import ax
from .cocoa import pooled

_MODS = [(1, "⇧"), (2, "⌥"), (4, "⌃")]  # AXMenuItemCmdModifiers bits; bit 3 (8) = no ⌘


def _shortcut(el) -> str:
    ch = ax._attr(el, "AXMenuItemCmdChar")
    if not ch:
        return ""
    mods = ax._attr(el, "AXMenuItemCmdModifiers") or 0
    try:
        mods = int(mods)
    except (TypeError, ValueError):
        mods = 0
    out = "".join(sym for bit, sym in _MODS if mods & bit)
    if not mods & 8:  # command is implied unless the "no command" bit is set
        out += "⌘"
    return out + str(ch).upper()


def _submenu(el):
    """The child AXMenu of a menu-bar item or a submenu row, if any."""
    for k in ax._attr(el, "AXChildren") or []:
        if str(ax._attr(k, "AXRole")) == "AXMenu":
            return k
    return None


def _row(pid: int, el, depth: int) -> dict:
    title = ax._attr(el, "AXTitle")
    sub = _submenu(el)
    if not title and sub is None:
        return {"separator": True}
    node: dict = {"title": str(title or "")}
    if ax._attr(el, "AXEnabled") not in (None, True, 1):
        node["disabled"] = True
    if ax._attr(el, "AXMenuItemMarkChar"):
        node["checked"] = True
    sc = _shortcut(el)
    if sc:
        node["shortcut"] = sc
    if sub is not None and depth < 6:  # container: recurse instead of pressing
        node["children"] = [_row(pid, c, depth + 1) for c in (ax._attr(sub, "AXChildren") or [])]
    else:
        node["id"] = ax.register(pid, el)  # leaf: pressable by id
    return node


@pooled
def read_menu(pid: int) -> list[dict]:
    """The app's menu bar as a tree of {title, id?, children?, disabled?,
    checked?, shortcut?, separator?}. Empty if the app exposes no menu bar."""
    mb = ax._attr(AXUIElementCreateApplication(pid), "AXMenuBar")
    if mb is None:
        return []
    return [_row(pid, it, 0) for it in (ax._attr(mb, "AXChildren") or [])]
