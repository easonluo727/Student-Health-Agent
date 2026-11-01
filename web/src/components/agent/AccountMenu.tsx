import { useEffect, useRef, useState } from "react";
import { LogOut } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { Auth } from "@/agent/useAuth";

const GSI_SRC = "https://accounts.google.com/gsi/client";

function loadGoogleScript(): Promise<void> {
  if (window.google?.accounts) return Promise.resolve();
  return new Promise((resolve, reject) => {
    let script = document.querySelector<HTMLScriptElement>(`script[src="${GSI_SRC}"]`);
    if (!script) {
      script = document.createElement("script");
      script.src = GSI_SRC;
      script.async = true;
      document.head.appendChild(script);
    }
    script.addEventListener("load", () => resolve(), { once: true });
    script.addEventListener("error", () => reject(new Error("Google sign-in could not load.")), { once: true });
  });
}

/** Header control: Google's own sign-in button, or the signed-in user with "Sign out". Hidden when sign-in is off. */
export function AccountMenu({ auth }: { auth: Auth }) {
  const button = useRef<HTMLDivElement>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const { user, clientId, ready, signIn } = auth;

  useEffect(() => {
    if (!ready || user || !clientId) return;
    let live = true;
    loadGoogleScript().then(() => {
      const google = window.google;
      if (!live || !google || !button.current) return;
      google.accounts.id.initialize({ client_id: clientId, callback: r => { void signIn(r.credential); } });
      google.accounts.id.renderButton(button.current, { type: "standard", theme: "outline", size: "large", shape: "pill", text: "signin_with" });
    }).catch(e => { if (live) setLoadError(e instanceof Error ? e.message : String(e)); });
    return () => { live = false; };
  }, [ready, user, clientId, signIn]);

  if (!ready || !clientId) return null;
  const error = auth.error ?? loadError;
  if (!user) return <div className="flex items-center gap-3">
    {error && <span role="alert" className="max-w-64 truncate text-xs text-negative-deep" title={error}>{error}</span>}
    <div ref={button} />
  </div>;

  return <div className="flex items-center gap-2">
    <span className="flex items-center gap-2 text-sm font-semibold" title={user.email}>
      {user.picture
        // eslint-disable-next-line @next/next/no-img-element
        ? <img src={user.picture} alt="" width={28} height={28} referrerPolicy="no-referrer" className="size-7 rounded-full" />
        : <span className="flex size-7 items-center justify-center rounded-full bg-secondary text-xs" aria-hidden>{(user.name ?? user.email)[0]?.toUpperCase()}</span>}
      <span className="max-w-40 truncate">{user.name ?? user.email}</span>
    </span>
    <Button variant="ghost" size="icon" aria-label="Sign out" title="Sign out" onClick={() => void auth.signOut()}><LogOut /></Button>
  </div>;
}
