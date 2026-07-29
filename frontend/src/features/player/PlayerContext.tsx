import {
  createContext,
  type ReactNode,
  useContext,
  useEffect,
  useState,
  useSyncExternalStore,
} from "react";

import { PlayerStore } from "./PlayerStore";
import type { PlayerSnapshot } from "./playerTypes";

const PlayerContext = createContext<PlayerStore | null>(null);

export function PlayerProvider({
  durationFallback,
  children,
}: {
  durationFallback: number;
  children: ReactNode;
}) {
  const [store] = useState(() => new PlayerStore(durationFallback));

  useEffect(() => {
    store.setDurationFallback(durationFallback);
  }, [durationFallback, store]);

  useEffect(() => () => store.reset(), [store]);

  return (
    <PlayerContext.Provider value={store}>
      {children}
    </PlayerContext.Provider>
  );
}
export function usePlayer(): PlayerStore {
  const store = useContext(PlayerContext);
  if (!store) {
    throw new Error("usePlayer must be used inside PlayerProvider.");
  }
  return store;
}

export function usePlayerSnapshot(): PlayerSnapshot {
  const store = usePlayer();
  return useSyncExternalStore(
    store.subscribe,
    store.getSnapshot,
    store.getSnapshot,
  );
}
