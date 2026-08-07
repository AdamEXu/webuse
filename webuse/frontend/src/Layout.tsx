import { useEffect, useState } from "react";
import { Outlet, useSearchParams } from "react-router-dom";
import Sidebar from "./views/Driver/Sidebar";

// Persistent debug-mode setting: outlines the ghost DOM (body.debug in index.css).
const DEBUG_KEY = "webuse:debug";

function readDebug(): boolean {
  try {
    return localStorage.getItem(DEBUG_KEY) === "1";
  } catch {
    return false;
  }
}

// The app shell: a sidebar that is always present (the primary navigator) and a
// #content area whose <Outlet/> swaps between the Info screen and app drivers.
export default function Layout() {
  const [debug, setDebug] = useState<boolean>(readDebug);
  const [search] = useSearchParams();

  // ?debug=1 stays a one-way "turn debug on" shortcut (e.g. an agent deep-linking
  // straight to /app/{pid}?debug=1). It only ever flips the setting on.
  useEffect(() => {
    if (search.get("debug") === "1") setDebug(true);
  }, [search]);

  // Persist the setting and apply it globally: the driver CSS outlines ghosts
  // under body.debug, so toggling it here affects every driver mounted below.
  useEffect(() => {
    try {
      localStorage.setItem(DEBUG_KEY, debug ? "1" : "0");
    } catch {
      /* storage unavailable */
    }
    document.body.classList.toggle("debug", debug);
    return () => document.body.classList.remove("debug");
  }, [debug]);

  return (
    <div id="shell">
      <Sidebar debug={debug} setDebug={setDebug} />
      <div id="content">
        <Outlet />
      </div>
    </div>
  );
}
