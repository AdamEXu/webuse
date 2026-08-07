"""Pydantic request models for the action API."""

from pydantic import BaseModel


class Step(BaseModel):
    action: str
    id: str | None = None  # ghost-DOM element id: its center becomes the target point
    value: str | None = None  # set_value text, or the literal string for `type`
    x: float | None = None  # window-relative target (server adds the window origin)
    y: float | None = None
    x2: float | None = None  # drag end point (window-relative)
    y2: float | None = None
    dx: float = 0.0  # scroll: wheel deltas in pixels
    dy: float = 0.0
    code: str | None = None  # key: KeyboardEvent.code (physical, layout-robust)
    down: bool | None = None  # key: True=down, False=up, None=tap (down+up)
    shift: bool = False  # key/click modifier flags (KeyboardEvent.*Key)
    ctrl: bool = False
    alt: bool = False
    meta: bool = False
    activate: bool = False  # raise the app to the foreground before this step


class Action(BaseModel):
    pid: int
    window_id: int | None = None  # which window to target; None => focused/main
    action: str | None = None  # single-step form (back-compat)
    id: str | None = None
    value: str | None = None
    x: float | None = None
    y: float | None = None
    x2: float | None = None
    y2: float | None = None
    dx: float = 0.0
    dy: float = 0.0
    code: str | None = None
    down: bool | None = None
    shift: bool = False
    ctrl: bool = False
    alt: bool = False
    meta: bool = False
    activate: bool = False
    actions: list[Step] | None = None  # batch form: steps run strictly in order


class Activate(BaseModel):
    pid: int
    window_id: int | None = None
