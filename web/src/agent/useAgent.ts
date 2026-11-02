import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import type { Answer, Attachment, EmailDraftEdit, Integrations, Plan, ReplyTarget, ServerEvent, SettingsUpdate, TurnContext, TurnInput } from "./protocol";
import type { AgentTransport } from "./transport";
import { agentReducer, initialState, type LocalAttachment } from "./store";
import { useAuth } from "./useAuth";

export function newSessionId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, c => { const r = (Math.random() * 16) | 0; return (c === "x" ? r : (r & 0x3) | 0x8).toString(16); });
}

export type ChatMode = "chat" | TurnContext["mode"];

export function useAgent(transport: AgentTransport) {
  const [state, dispatch] = useReducer(agentReducer, undefined, () => initialState(newSessionId()));
  const auth = useAuth(transport);
  const [plans, setPlans] = useState<Plan[]>([]);
  const [integrations, setIntegrations] = useState<Integrations | null>(null);
  const [replyTarget, setReplyTarget] = useState<ReplyTarget | null>(null);
  const [mode, setMode] = useState<ChatMode>("chat");
  const [planIds, setPlanIds] = useState<string[]>([]);
  const abort = useRef<AbortController | null>(null);
  const sessionRef = useRef(state.sessionId);
  useEffect(() => { sessionRef.current = state.sessionId; }, [state.sessionId]);

  const refreshPlans = useCallback(async () => {
    try { setPlans(await transport.getPlans()); } catch { setPlans([]); }
  }, [transport]);
  useEffect(() => {
    let live = true;
    transport.getPlans().then(p => { if (live) setPlans(p); }).catch(() => { if (live) setPlans([]); });
    return () => { live = false; };
  }, [transport]);
  const refreshIntegrations = useCallback(async () => {
    try { const i = await transport.getIntegrations(); setIntegrations(i); return i; } catch { return null; }
  }, [transport]);
  useEffect(() => {
    let live = true;
    transport.getIntegrations().then(i => { if (live) setIntegrations(i); }).catch(() => {});
    return () => { live = false; };
  }, [transport]);
  useEffect(() => () => abort.current?.abort(), []);

  const run = useCallback(async (input: TurnInput, target?: ReplyTarget | null) => {
    abort.current?.abort();
    const ctrl = new AbortController();
    abort.current = ctrl;
    dispatch({ type: "busy", busy: true });
    const context: TurnContext | undefined = mode === "chat" ? undefined : planIds.length ? { mode, plan_ids: planIds } : { mode };
    try {
      for await (const event of transport.turn({ session_id: sessionRef.current, input, ...(target ? { reply_target: target } : {}), ...(context ? { context } : {}) }, ctrl.signal)) {
        if (ctrl.signal.aborted) break;
        dispatch({ type: "server", event });
      }
      if (!ctrl.signal.aborted && input.type === "message" && input.attachments?.length) await refreshPlans();
    } catch (e) {
      if (!ctrl.signal.aborted) dispatch({ type: "local_error", message: e instanceof Error ? e.message : "The agent could not be reached." });
    } finally {
      if (abort.current === ctrl) { abort.current = null; dispatch({ type: "busy", busy: false }); }
    }
  }, [transport, mode, planIds, refreshPlans]);

  const sendMessage = useCallback((text: string, attachments: LocalAttachment[] = []) => {
    const target = replyTarget;
    dispatch({ type: "user", item: { kind: "user", id: `u-${Date.now()}`, text, attachments, replyTarget: target ?? undefined } });
    setReplyTarget(null);
    const atts: Attachment[] = attachments.map(a => ({ file_id: a.file_id, name: a.name }));
    return run(atts.length ? { type: "message", text, attachments: atts } : { type: "message", text }, target);
  }, [run, replyTarget]);

  const submitForm = useCallback((requestId: string, answers: Record<string, Answer>, summary: string) => {
    dispatch({ type: "form", requestId, state: "submitted", summary });
    dispatch({ type: "user", item: { kind: "user", id: `u-${Date.now()}`, text: summary, attachments: [] } });
    return run({ type: "form_response", request_id: requestId, answers });
  }, [run]);
  const dismissForm = useCallback((requestId: string) => {
    dispatch({ type: "form", requestId, state: "dismissed" });
    return run({ type: "form_dismiss", request_id: requestId });
  }, [run]);
  const respondConfirm = useCallback((requestId: string, confirmed: boolean, correction?: string) => {
    dispatch({ type: "confirm", requestId, state: confirmed ? "confirmed" : "corrected", correction });
    return run(!confirmed && correction ? { type: "confirm_response", request_id: requestId, confirmed, correction } : { type: "confirm_response", request_id: requestId, confirmed });
  }, [run]);
  const sendEmail = useCallback((requestId: string, draft: EmailDraftEdit) => {
    dispatch({ type: "email", requestId, draft, status: { request_id: requestId, status: "sending" } });
    return run({ type: "email_response", request_id: requestId, action: "send", draft });
  }, [run]);
  const cancelEmail = useCallback((requestId: string) => {
    dispatch({ type: "email", requestId, state: "cancelled" });
    return run({ type: "email_response", request_id: requestId, action: "cancel" });
  }, [run]);
  const upload = useCallback((file: File) => transport.upload(sessionRef.current, file), [transport]);

  const stop = useCallback(() => { abort.current?.abort(); abort.current = null; dispatch({ type: "busy", busy: false }); }, []);
  const newChat = useCallback(() => {
    stop();
    setReplyTarget(null);
    setMode("chat");
    setPlanIds([]);
    const sessionId = newSessionId();
    sessionRef.current = sessionId;
    dispatch({ type: "reset", sessionId });
  }, [stop]);

  // A signed-in user's conversation is theirs alone (the backend answers 404 to anyone else), so signing out starts
  // a new chat. Signing in keeps the current one: the backend gives it to the account on the next turn.
  const userId = auth.user?.user_id ?? null;
  const previousUserId = useRef(userId);
  useEffect(() => {
    if (previousUserId.current !== null && previousUserId.current !== userId) newChat();
    previousUserId.current = userId;
  }, [userId, newChat]);

  /** Reopen one of the signed-in user's saved chats; the next message continues it. Throws if it can't be loaded. */
  const openSession = useCallback(async (sessionId: string) => {
    const saved = await transport.getSession(sessionId);
    stop();
    setReplyTarget(null);
    setMode("chat");
    setPlanIds([]);
    const files = Object.fromEntries(saved.uploads.map(u => [u.file_id, {
      file_id: u.file_id, name: u.name, size: u.size, content_type: u.content_type,
      preview: u.content_type.startsWith("image/") ? transport.fileUrl(u.file_id) ?? undefined : undefined,
    }]));
    sessionRef.current = sessionId;
    dispatch({ type: "restore", sessionId, events: saved.events, files });
  }, [transport, stop]);
  const listSessions = useCallback(() => transport.listSessions(), [transport]);
  const listUploads = useCallback(() => transport.listUploads(), [transport]);
  const getSettings = useCallback(() => transport.getSettings(), [transport]);
  const { updateUser } = auth;
  const saveSettings = useCallback(async (update: SettingsUpdate) => {
    const saved = await transport.saveSettings(update);
    updateUser(saved.user);
    return saved;
  }, [transport, updateUser]);
  const fileUrl = useCallback((fileId: string) => transport.fileUrl(fileId), [transport]);
  const deleteSession = useCallback(async (sessionId: string) => {
    await transport.deleteSession(sessionId);
    if (sessionId === sessionRef.current) newChat();
  }, [transport, newChat]);

  /** Dev/mock only: replay recorded events into a fresh conversation without a network call. */
  const replay = useCallback((events: ServerEvent[], opts: { userText?: string | undefined; attachments?: LocalAttachment[] | undefined; busy?: boolean | undefined; view?: string | undefined } = {}) => {
    stop();
    setReplyTarget(null);
    dispatch({ type: "reset", sessionId: newSessionId() });
    if (opts.userText) dispatch({ type: "user", item: { kind: "user", id: "u-replay", text: opts.userText, attachments: opts.attachments ?? [] } });
    for (const event of events) dispatch({ type: "server", event });
    if (opts.busy) dispatch({ type: "busy", busy: true });
    if (opts.view) dispatch({ type: "canvas", open: true, view: opts.view });
  }, [stop]);

  return useMemo(() => ({
    state, auth, plans, integrations, replyTarget, mode, planIds,
    setReplyTarget, setMode, setPlanIds, refreshIntegrations,
    setCanvas: (open?: boolean, view?: string) => dispatch({ type: "canvas", open, view }),
    sendMessage, submitForm, dismissForm, respondConfirm, sendEmail, cancelEmail, upload, stop, newChat, replay,
    openSession, listSessions, deleteSession, listUploads, getSettings, saveSettings, fileUrl,
  }), [state, auth, plans, integrations, replyTarget, mode, planIds, refreshIntegrations, sendMessage, submitForm, dismissForm, respondConfirm, sendEmail, cancelEmail, upload, stop, newChat, replay, openSession, listSessions, deleteSession, listUploads, getSettings, saveSettings, fileUrl]);
}

export type Agent = ReturnType<typeof useAgent>;
