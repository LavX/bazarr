/**
 * Guarded access to the browser's two web storage areas.
 *
 * Reading or writing site data is not always allowed: a locked-down profile, a
 * private window, or a browser configured to block storage all throw on the
 * very first access rather than returning null. That applies to `sessionStorage`
 * exactly as it applies to `localStorage`, and both are blocked together by the
 * settings readers actually use, so guarding one and not the other leaves the
 * page just as broken. Anything that runs while the application shell mounts has
 * to survive it, because a throw there takes the whole page down before the
 * reader sees anything at all.
 *
 * These helpers never throw. A read that cannot happen is indistinguishable
 * from nothing stored, and a write that cannot happen reports it so a caller
 * that has something honest to say about the session can say it.
 */
function read(area: () => Storage, key: string): string | null {
  try {
    return area().getItem(key);
  } catch {
    return null;
  }
}

function write(area: () => Storage, key: string, value: string): boolean {
  try {
    area().setItem(key, value);
    return true;
  } catch {
    return false;
  }
}

function remove(area: () => Storage, key: string): void {
  try {
    area().removeItem(key);
  } catch {
    // Nothing readable also means nothing to remove.
  }
}

// The areas are reached through a getter rather than captured once, because the
// property access itself is what throws in a browser that blocks site data.
const local = () => localStorage;
const session = () => sessionStorage;

export function readStoredValue(key: string): string | null {
  return read(local, key);
}

export function writeStoredValue(key: string, value: string): boolean {
  return write(local, key, value);
}

export function removeStoredValue(key: string): void {
  remove(local, key);
}

export function readSessionValue(key: string): string | null {
  return read(session, key);
}

export function writeSessionValue(key: string, value: string): boolean {
  return write(session, key, value);
}

export function removeSessionValue(key: string): void {
  remove(session, key);
}
