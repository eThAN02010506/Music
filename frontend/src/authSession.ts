export const AUTH_SYNC_STORAGE_KEY = "music-insight:auth-session-changed";

type StorageWriter = Pick<Storage, "setItem">;

export function announceAuthChange(
  storage: StorageWriter | null = typeof window !== "undefined"
    ? window.localStorage
    : null,
  token = `${Date.now()}:${Math.random()}`,
): void {
  try {
    storage?.setItem(AUTH_SYNC_STORAGE_KEY, token);
  } catch {
    // Login/logout must still work when storage is unavailable or disabled.
  }
}

export function isAuthSyncStorageKey(key: string | null): boolean {
  return key === AUTH_SYNC_STORAGE_KEY;
}
