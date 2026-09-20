"use client";

import { useSyncExternalStore } from "react";

// localStorage-backed values read during render (no setState-in-effect, no hydration mismatch):
// the server snapshot is the fallback; writes notify every subscriber in this tab and other tabs.
const EVENT = "vx-storage";

function subscribe(cb: () => void) {
  window.addEventListener("storage", cb);
  window.addEventListener(EVENT, cb);
  return () => {
    window.removeEventListener("storage", cb);
    window.removeEventListener(EVENT, cb);
  };
}

function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function writeStored(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* private mode: not remembered */
  }
  window.dispatchEvent(new Event(EVENT));
}

export function useStoredString(key: string, fallback: string): string {
  return useSyncExternalStore(subscribe, () => read(key) ?? fallback, () => fallback);
}

export function useStoredNumber(key: string, fallback: number): number {
  const raw = useStoredString(key, String(fallback));
  const n = Number(raw);
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

/** Presentation mode: bigger type, no navigation, step through a result (see ResultView, globals.css). */
export const PRESENT_KEY = "vx-present";

export function applyPresent(on: boolean) {
  if (on) document.documentElement.dataset.present = "on";
  else delete document.documentElement.dataset.present;
  writeStored(PRESENT_KEY, on ? "on" : "off");
}
