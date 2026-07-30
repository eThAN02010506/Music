import {
  useLayoutEffect,
  useRef,
  type MouseEvent,
  type ReactNode,
} from "react";

type InertSnapshot = {
  count: number;
  inert: boolean;
  ariaHidden: string | null;
};

const inertSnapshots = new WeakMap<HTMLElement, InertSnapshot>();

function acquireInert(element: HTMLElement): void {
  const existing = inertSnapshots.get(element);
  if (existing) {
    existing.count += 1;
    return;
  }
  inertSnapshots.set(element, {
    count: 1,
    inert: element.inert,
    ariaHidden: element.getAttribute("aria-hidden"),
  });
  element.inert = true;
  element.setAttribute("aria-hidden", "true");
}

function releaseInert(element: HTMLElement): void {
  const snapshot = inertSnapshots.get(element);
  if (!snapshot) return;
  snapshot.count -= 1;
  if (snapshot.count > 0) return;
  element.inert = snapshot.inert;
  if (snapshot.ariaHidden === null) {
    element.removeAttribute("aria-hidden");
  } else {
    element.setAttribute("aria-hidden", snapshot.ariaHidden);
  }
  inertSnapshots.delete(element);
}

function makeBackgroundInert(backdrop: HTMLElement): () => void {
  const acquired: HTMLElement[] = [];
  let current: HTMLElement = backdrop;
  while (current.parentElement) {
    const parent = current.parentElement;
    for (const sibling of Array.from(parent.children)) {
      if (
        sibling === current
        || !(sibling instanceof HTMLElement)
        || sibling.tagName === "SCRIPT"
        || sibling.tagName === "STYLE"
      ) continue;
      acquireInert(sibling);
      acquired.push(sibling);
    }
    if (parent === document.body) break;
    current = parent;
  }
  return () => {
    for (const element of acquired.reverse()) releaseInert(element);
  };
}

export function ModalDialog({
  titleId,
  panelClassName = "",
  onClose,
  children,
}: {
  titleId: string;
  panelClassName?: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const backdropRef = useRef<HTMLDivElement>(null);
  const dialogRef = useRef<HTMLElement>(null);
  const onCloseRef = useRef(onClose);

  useLayoutEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useLayoutEffect(() => {
    const focusableSelector =
      'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
    const previousFocus = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const dialog = dialogRef.current;
      if (!dialog) return;
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(
        focusableSelector,
      )).filter((element) => !element.hasAttribute("hidden"));
      if (!focusable.length) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const current = document.activeElement;
      if (event.shiftKey && (current === first || !dialog.contains(current))) {
        event.preventDefault();
        last.focus();
      } else if (
        !event.shiftKey
        && (current === last || !dialog.contains(current))
      ) {
        event.preventDefault();
        first.focus();
      }
    };
    const dialog = dialogRef.current;
    const backdrop = backdropRef.current;
    const firstFocus = dialog?.querySelector<HTMLElement>(focusableSelector);
    (firstFocus || dialog)?.focus();
    const releaseBackground = backdrop
      ? makeBackgroundInert(backdrop)
      : () => {};
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      releaseBackground();
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  }, []);

  const closeFromBackdrop = (event: MouseEvent<HTMLDivElement>) => {
    if (event.target === event.currentTarget) onClose();
  };

  return (
    <div
      ref={backdropRef}
      className="dialog-backdrop"
      role="presentation"
      onMouseDown={closeFromBackdrop}
    >
      <section
        ref={dialogRef}
        className={`dialog-panel ${panelClassName}`.trim()}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
      >
        {children}
      </section>
    </div>
  );
}
