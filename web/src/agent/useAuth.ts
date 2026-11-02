import { useCallback, useEffect, useMemo, useState } from "react";
import type { User } from "./protocol";
import type { AgentTransport } from "./transport";

/** Signed-in user and Google client id from the backend; `ready` once the first check has answered. */
export function useAuth(transport: AgentTransport) {
  const [user, setUser] = useState<User | null>(null);
  const [clientId, setClientId] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    transport.getAuth()
      .then(a => { if (live) { setUser(a.user); setClientId(a.google_client_id); } })
      .catch(() => {})
      .finally(() => { if (live) setReady(true); });
    return () => { live = false; };
  }, [transport]);

  const signIn = useCallback(async (credential: string) => {
    setError(null);
    try { setUser(await transport.signInWithGoogle(credential)); }
    catch (e) { setError(e instanceof Error ? e.message : "Google sign-in failed."); }
  }, [transport]);

  const signOut = useCallback(async () => {
    try { await transport.signOut(); } catch { /* the cookie may already be gone */ }
    window.google?.accounts.id.disableAutoSelect();
    setUser(null);
  }, [transport]);

  // After the user changes their name in Settings.
  const updateUser = useCallback((u: User) => setUser(u), []);

  return useMemo(() => ({ user, clientId, ready, error, signIn, signOut, updateUser }), [user, clientId, ready, error, signIn, signOut, updateUser]);
}

export type Auth = ReturnType<typeof useAuth>;

// The part of Google Identity Services (https://accounts.google.com/gsi/client) used by AccountMenu.
declare global {
  interface Window {
    google?: {
      accounts: {
        id: {
          initialize(options: { client_id: string; callback: (response: { credential: string }) => void; auto_select?: boolean }): void;
          renderButton(parent: HTMLElement, options: Record<string, string | number>): void;
          disableAutoSelect(): void;
        };
      };
    };
  }
}
