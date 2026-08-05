/**
 * Hide the embedded browser's native view while a renderer overlay is up.
 *
 * The browser pane is an Electron `WebContentsView` painted over a measured
 * placeholder; it is a sibling of the renderer's view, so it covers the entire
 * DOM regardless of z-index. Any overlay that must be visible (every dialog,
 * the image lightbox, toasts) asks the shell to detach the view while it is
 * shown.
 *
 * Suppression is ref-counted across all callers so overlapping overlays (a
 * toast over a dialog, a dialog opened from another) restore the view exactly
 * once, when the last one closes.
 */
import { useEffect } from "react";
import { setBrowserOverlaySuppressed } from "@/lib/nativeBridge";

let refCount = 0;

/** Acquire a suppression lease; returns a release function (idempotent). */
export function acquireBrowserViewSuppression(): () => void {
  refCount += 1;
  if (refCount === 1) void setBrowserOverlaySuppressed(true);
  let released = false;
  return () => {
    if (released) return;
    released = true;
    refCount = Math.max(0, refCount - 1);
    if (refCount === 0) void setBrowserOverlaySuppressed(false);
  };
}

/** Suppress the native browser view while `active` is true. */
export function useSuppressBrowserView(active: boolean): void {
  useEffect(() => {
    if (!active) return;
    return acquireBrowserViewSuppression();
  }, [active]);
}
