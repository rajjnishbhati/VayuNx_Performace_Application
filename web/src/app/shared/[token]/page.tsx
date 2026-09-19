"use client";

import { use, useEffect, useState } from "react";
import ResultView from "@/components/ResultView";
import { ErrorState, Skeleton } from "@/components/States";
import { ApiError, api } from "@/lib/api";
import { fmtDate } from "@/lib/format";
import type { CompareResult, Timeseries } from "@/lib/types";

type Shared = CompareResult & { shared: { expires_at: string; created_at: string } };

/** A comparison someone shared: read-only, no sign-in needed, until the link expires. */
export default function SharedPage({ params }: { params: Promise<{ token: string }> }) {
  const { token } = use(params);
  const [result, setResult] = useState<Shared | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [ts, setTs] = useState<Timeseries | null>(null);
  const [tsLoading, setTsLoading] = useState(true);

  useEffect(() => {
    let live = true;
    api.shared(token).then((r) => {
      if (!live) return;
      setResult(r);
      if (r.experiment) {
        api.sharedTimeseries(token).then((t) => live && setTs(t)).catch(() => undefined).finally(() => live && setTsLoading(false));
      } else {
        setTsLoading(false);
      }
    }).catch((e) => live && setError(e as ApiError));
    return () => { live = false; };
  }, [token]);

  return (
    <div className="page">
      <h1>Shared comparison</h1>
      {error ? <ErrorState error={error.message} fix={error.fix} /> : !result ? <Skeleton lines={6} /> : (
        <>
          <p className="muted" role="note">
            Read-only view shared from VAYUNX Crypto Profiler · shared {fmtDate(result.shared.created_at)} · this link
            expires {fmtDate(result.shared.expires_at)}.
          </p>
          <ResultView result={result} ts={ts} tsLoading={tsLoading} sharedToken={token} />
        </>
      )}
    </div>
  );
}
