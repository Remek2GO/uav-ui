type Callback = (lat: number, lon: number) => void;

let pending: Callback | null = null;
const listeners = new Set<() => void>();

/** Lets a custom panel ask for the next click on the map. */
export function requestMapClick(callback: Callback) {
  pending = callback;
  listeners.forEach((listener) => listener());
}

export function hasPendingMapClick() {
  return pending !== null;
}

export function takeMapClick(): Callback | null {
  const callback = pending;
  pending = null;
  listeners.forEach((listener) => listener());
  return callback;
}

export function subscribeMapClick(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
