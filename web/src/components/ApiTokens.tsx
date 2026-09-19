"use client";

import { useCallback, useEffect, useState } from "react";
import { ErrorState } from "@/components/States";
import { ApiError, api } from "@/lib/api";
import type { ApiTokenItem, Me } from "@/lib/types";

/** Settings: API tokens for SDKs and CI (shown only when the Service requires sign-in). */
export default function ApiTokens({ me }: { me: Me }) {
  const [tokens, setTokens] = useState<ApiTokenItem[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [name, setName] = useState("");
  const [created, setCreated] = useState<{ name: string; token: string } | null>(null);
  const load = useCallback(() => { api.tokens().then(setTokens).catch(setError); }, []);
  useEffect(() => { load(); }, [load]);

  const create = async () => {
    try {
      const t = await api.createToken(name.trim());
      setCreated({ name: t.name, token: t.token });
      setName("");
      load();
    } catch (e) {
      setError(e as ApiError);
    }
  };
  const revoke = async (id: string) => {
    try {
      await api.revokeToken(id);
      load();
    } catch (e) {
      setError(e as ApiError);
    }
  };

  return (
    <section className="card" aria-labelledby="tokens-title">
      <h2 id="tokens-title">API tokens</h2>
      <p className="ink2">
        SDKs and CI send a token instead of signing in: set <code>VAYUNX_API_TOKEN</code> where they run. A token acts as
        you{me.is_admin ? " (an administrator)" : ""}; revoke it when it is no longer needed.
      </p>
      {error && <ErrorState error={error.message} fix={error.fix} onRetry={() => { setError(null); load(); }} />}
      {created && (
        <div role="status" className="card" style={{ background: "var(--surface-2)" }}>
          <p style={{ margin: "0 0 6px" }}><strong>{created.name}</strong>: copy this token now. It is not shown again.</p>
          <pre className="code-block" style={{ userSelect: "all" }}>{created.token}</pre>
          <div className="row" style={{ gap: 8 }}>
            <button onClick={() => { navigator.clipboard?.writeText(created.token); }}>Copy</button>
            <button onClick={() => setCreated(null)}>Done</button>
          </div>
        </div>
      )}
      <form className="row" style={{ gap: 8, alignItems: "end", margin: "8px 0 12px" }}
            onSubmit={(e) => { e.preventDefault(); if (name.trim()) create(); }}>
        <label className="field">Name (what is it for?)
          <input value={name} maxLength={128} onChange={(e) => setName(e.target.value)} placeholder="e.g. ci-login-gate" />
        </label>
        <button className="btn-primary" type="submit" disabled={!name.trim()}>Create token</button>
      </form>
      <div className="scroll-x">
        <table className="data">
          <thead>
            <tr><th scope="col">Name</th><th scope="col">Starts with</th><th scope="col">Created</th><th scope="col">Last used</th>
              <th scope="col"><span className="sr-only">Actions</span></th></tr>
          </thead>
          <tbody>
            {!tokens && <tr><td colSpan={5}><div className="skeleton" style={{ height: 18 }} aria-hidden /></td></tr>}
            {tokens?.length === 0 && <tr><td colSpan={5} className="muted">No tokens yet.</td></tr>}
            {tokens?.map((t) => (
              <tr key={t.token_id}>
                <th scope="row" style={{ fontWeight: 500 }}>{t.name}</th>
                <td className="mono">{t.prefix}…</td>
                <td className="muted">{new Date(t.created_at).toLocaleString()}</td>
                <td className="muted">{t.last_used_at ? new Date(t.last_used_at).toLocaleString() : "never"}</td>
                <td>{t.revoked ? <span className="muted">revoked</span> : <button onClick={() => revoke(t.token_id)}>Revoke</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
