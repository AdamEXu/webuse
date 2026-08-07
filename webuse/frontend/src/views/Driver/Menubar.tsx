import { useEffect, useRef } from "react";
import { API, type MenuItem, type Step } from "../../api";

// Web menu bar: the app's real menus, rendered client-side. Only the chosen item
// is sent to the app (as an AXPress of its element id). Ported 1:1 from APP_PAGE —
// kept imperative (body-appended fixed submenus + hover) to preserve the behavior.
export default function Menubar({
  handle,
  act,
}: {
  handle: string;
  act: (payload: Step) => void;
}) {
  const menubarRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const menubarEl = menubarRef.current;
    if (!menubarEl) return;

    let menuStack: HTMLDivElement[] = []; // open .menu divs, outer-first
    let openTop: HTMLDivElement | null = null; // currently-open top-level .top

    function closeMenus(n: number) {
      while (menuStack.length > n) menuStack.pop()!.remove();
      if (n === 0 && openTop) {
        openTop.classList.remove("open");
        openTop = null;
      }
    }

    function buildMenu(items: MenuItem[], level: number, x: number, y: number) {
      closeMenus(level); // after this, menuStack.length === level
      const m = document.createElement("div");
      m.className = "menu";
      for (const it of items) {
        if (it.separator) {
          const s = document.createElement("div");
          s.className = "sep";
          m.appendChild(s);
          continue;
        }
        const el = document.createElement("div");
        el.className =
          "item" + (it.disabled ? " disabled" : "") + (it.children ? " sub" : "");
        const lbl = document.createElement("span");
        lbl.className = "lbl";
        lbl.textContent = (it.checked ? "✓ " : "") + (it.title || "");
        el.appendChild(lbl);
        if (it.shortcut || it.children) {
          const sc = document.createElement("span");
          sc.className = "sc";
          sc.textContent = it.shortcut || "";
          el.appendChild(sc);
        }
        m.appendChild(el);
        if (it.disabled) continue;
        if (it.children) {
          const children = it.children;
          el.addEventListener("mouseenter", () => {
            const r = el.getBoundingClientRect();
            buildMenu(children, level + 1, r.right - 5, r.top - 5);
          });
        } else {
          el.addEventListener("mouseenter", () => closeMenus(level + 1));
          if (it.id) {
            const id = it.id;
            el.addEventListener("click", (e) => {
              e.stopPropagation();
              closeMenus(0);
              act({ id, action: "press" });
            });
          }
        }
      }
      document.body.appendChild(m);
      const w = m.offsetWidth;
      if (x + w > window.innerWidth) x = Math.max(2, window.innerWidth - w - 4);
      m.style.left = x + "px";
      m.style.top = y + "px";
      menuStack[level] = m;
      return m;
    }

    function openTopMenu(topEl: HTMLDivElement, item: MenuItem) {
      closeMenus(0);
      topEl.classList.add("open");
      openTop = topEl;
      const r = topEl.getBoundingClientRect();
      buildMenu(item.children || [], 0, r.left, r.bottom + 2);
    }

    function renderMenubar(tree: MenuItem[]) {
      menubarEl!.replaceChildren();
      for (const top of tree) {
        if (!top.title || !top.children) continue;
        const t = document.createElement("div");
        t.className = "top";
        t.textContent = top.title;
        t.addEventListener("click", (e) => {
          e.stopPropagation();
          if (openTop === t) closeMenus(0);
          else openTopMenu(t, top);
        });
        t.addEventListener("mouseenter", () => {
          if (openTop && openTop !== t) openTopMenu(t, top);
        });
        menubarEl!.appendChild(t);
      }
    }

    function onDocClick() {
      closeMenus(0);
    }
    document.addEventListener("click", onDocClick);

    async function loadMenu() {
      if (menuStack.length) return; // don't rebuild while a menu is open
      try {
        const r = await fetch(API.menu(handle));
        if (r.ok) {
          const j = (await r.json()) as { menu?: MenuItem[] };
          renderMenubar(j.menu || []);
        }
      } catch {
        /* a missing/odd menu bar must not break the page */
      }
    }
    loadMenu();
    const iv = setInterval(loadMenu, 5000);

    return () => {
      clearInterval(iv);
      document.removeEventListener("click", onDocClick);
      closeMenus(0);
      menuStack = [];
    };
  }, [handle, act]);

  return <div id="menubar" ref={menubarRef} />;
}
