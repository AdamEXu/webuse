// Ghost-DOM rendering helpers, ported verbatim from APP_PAGE.
//
// These elements let agents read control labels/positions AND act: each ghost is
// clickable (pointer-events:auto) and a click on it forwards a real HID click to
// the app (see useDriver's onGhostClick); interaction is always real HID input,
// never a DOM event driving the ghost itself. Updates are write-guarded and keyed
// by stable ax id so element identity (and any references an agent holds) survives
// refreshes untouched.
import type { GhostNode } from "../../api";

// Never touch the DOM when a value is already correct.
export function setAttr(e: Element, k: string, v: string | null | undefined): void {
  if (v == null || v === "") {
    if (e.hasAttribute(k)) e.removeAttribute(k);
  } else if (e.getAttribute(k) !== String(v)) {
    e.setAttribute(k, v);
  }
}

export function setStyle(e: HTMLElement, k: string, v: string): void {
  if (e.style.getPropertyValue(k) !== v) e.style.setProperty(k, v);
}

export function build(n: GhostNode, existing?: HTMLElement): HTMLElement {
  let e = existing;
  if (!e) {
    // invariants for a given id+tag; sync() recreates on tag change
    e = document.createElement(n.tag);
    e.className = "ghost";
    e.dataset.axId = n.id;
    e.dataset.kind = n.kind;
    if (n.tag === "input") (e as HTMLInputElement).type = n.type || "text"; // checkbox/radio only
    if (n.tag === "a") (e as HTMLAnchorElement).href = "#";
    if (n.kind === "text") {
      // Text fields mirror as a non-editable, non-focusable div: no tabindex,
      // not contenteditable, no .value sink — so programmatic "typing" (direct
      // .value writes, insertText, execCommand) has nowhere to land except the
      // sanctioned real-HID path via the stage.
      e.setAttribute("role", "textbox");
      e.setAttribute("aria-readonly", "true");
    }
  }
  // AX truth always wins — no activeElement guard, so anything injected into a
  // ghost snaps back to the app's real value on the next refresh.
  if (n.kind === "text") {
    const v = n.value ?? "";
    if (e.textContent !== v) e.textContent = v;
    setAttr(e, "data-value", v);
  } else if (n.tag === "input") {
    const inp = e as HTMLInputElement; // checkbox/radio
    if (inp.checked !== !!n.checked) inp.checked = !!n.checked;
  } else {
    const t = n.text || "";
    if (e.textContent !== t) e.textContent = t;
  }
  setAttr(e, "data-role", n.role); // raw AXRole: disambiguates within a kind
  setAttr(e, "aria-label", n.label);
  setAttr(e, "title", n.help || n.label); // AXHelp tooltip when present
  setAttr(e, n.kind === "text" ? "aria-placeholder" : "placeholder", n.placeholder);
  if (n.tag === "input" || n.tag === "button") {
    const d = e as HTMLInputElement | HTMLButtonElement;
    if (d.disabled !== !!n.disabled) d.disabled = !!n.disabled;
  }
  setStyle(e, "left", n.x + "px");
  setStyle(e, "top", n.y + "px");
  setStyle(e, "width", n.w + "px");
  setStyle(e, "height", n.h + "px");
  return e;
}

// Update in place, keyed by stable ax id, so element identity survives refreshes.
export function sync(
  container: HTMLElement,
  nodes: GhostNode[],
  lastNode: Map<string, string>,
): void {
  const byId = new Map<string, HTMLElement>();
  for (const el of Array.from(container.children)) {
    const he = el as HTMLElement;
    const id = he.dataset.axId;
    if (id !== undefined) byId.set(id, he);
  }
  const seen = new Set<string>();
  for (const n of nodes) {
    seen.add(n.id);
    const existing = byId.get(n.id);
    const key = JSON.stringify(n);
    if (existing && existing.tagName.toLowerCase() === n.tag) {
      if (lastNode.get(n.id) === key) continue; // unchanged: don't touch it
      build(n, existing);
    } else {
      if (existing) existing.remove();
      container.appendChild(build(n));
    }
    lastNode.set(n.id, key);
  }
  for (const [id, el] of byId) {
    if (!seen.has(id)) {
      el.remove();
      lastNode.delete(id);
    }
  }
}
