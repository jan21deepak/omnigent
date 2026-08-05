// The embedded browser's native view paints above the whole renderer DOM, so
// overlays ask the shell to hide it. These tests pin the ref-counting and the
// shared DialogOverlay wiring.
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { useSuppressBrowserView } from "./useSuppressBrowserView";

const calls: boolean[] = [];

function installShell(): void {
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    browserOpenOrNavigate: () => Promise.resolve({ ok: true }),
    browserSetOverlaySuppressed: (suppressed: boolean) => {
      calls.push(suppressed);
      return Promise.resolve({ ok: true, suppressed });
    },
  };
}

beforeEach(() => {
  calls.length = 0;
  installShell();
});

afterEach(() => {
  cleanup();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  vi.restoreAllMocks();
});

function Overlay({ active }: { active: boolean }) {
  useSuppressBrowserView(active);
  return null;
}

describe("useSuppressBrowserView", () => {
  it("suppresses while active and restores on unmount", () => {
    const view = render(<Overlay active />);
    expect(calls).toEqual([true]);
    view.unmount();
    expect(calls).toEqual([true, false]);
  });

  it("does nothing while inactive", () => {
    render(<Overlay active={false} />);
    expect(calls).toEqual([]);
  });

  it("ref-counts overlapping overlays: one suppress, one restore", () => {
    const first = render(<Overlay active />);
    const second = render(<Overlay active />);
    expect(calls).toEqual([true]);
    first.unmount();
    expect(calls).toEqual([true]); // still suppressed while an overlay remains
    second.unmount();
    expect(calls).toEqual([true, false]);
  });
});

describe("DialogOverlay browser-view suppression", () => {
  it("suppresses the native view for any dialog, and restores when it closes", () => {
    const view = render(
      <Dialog open onOpenChange={() => {}}>
        <DialogContent>
          <DialogTitle>Fork</DialogTitle>
        </DialogContent>
      </Dialog>,
    );
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(calls).toEqual([true]);
    view.rerender(
      <Dialog open={false} onOpenChange={() => {}}>
        <DialogContent>
          <DialogTitle>Fork</DialogTitle>
        </DialogContent>
      </Dialog>,
    );
    expect(calls).toEqual([true, false]);
  });
});
