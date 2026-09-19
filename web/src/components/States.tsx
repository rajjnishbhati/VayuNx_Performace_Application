"use client";

export function Skeleton({ lines = 4 }: { lines?: number }) {
  return (
    <div className="card" aria-busy="true" aria-label="Loading results">
      <div className="skeleton" style={{ height: 26, width: "70%", marginBottom: 16 }} />
      {Array.from({ length: lines }).map((_, i) => (
        <div key={i} className="skeleton" style={{ height: 16, width: `${90 - i * 12}%`, marginBottom: 10 }} />
      ))}
    </div>
  );
}

export function ErrorState({ error, fix, onRetry }: { error: string; fix?: string; onRetry?: () => void }) {
  return (
    <div className="card" role="alert">
      <h2>Something went wrong</h2>
      <p><b>What happened:</b> {error}</p>
      {fix && <p><b>How to fix it:</b> {fix}</p>}
      {onRetry && <button onClick={onRetry}>Retry</button>}
    </div>
  );
}

export function EmptyState({ onPick }: { onPick: (presets: string[]) => void }) {
  return (
    <div className="card">
      <h2>Compare your first two algorithms</h2>
      <p className="ink2">
        Pick the algorithm you use today and the one you are considering. The Lab hashes a synthetic password on this
        machine — five 10-second trials each — and shows what the switch costs per call, in CPU and in memory.
      </p>
      <div className="row">
        <button className="btn-primary" onClick={() => onPick(["md5", "argon2id-rfc9106-low"])}>MD5 → Argon2id</button>
        <button onClick={() => onPick(["md5", "bcrypt-12"])}>MD5 → bcrypt</button>
        <button onClick={() => onPick(["sha256", "argon2id-owasp"])}>SHA-256 → Argon2id (OWASP minimum)</button>
      </div>
    </div>
  );
}
