"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import AlgorithmPicker from "@/components/AlgorithmPicker";
import AppRunsPicker from "@/components/AppRunsPicker";
import LabProgress from "@/components/LabProgress";
import ResultView from "@/components/ResultView";
import { EmptyState, ErrorState, Skeleton } from "@/components/States";
import { ApiError, api } from "@/lib/api";
import type { CompareResult, Experiment, Preset, Timeseries } from "@/lib/types";
import { useStoredNumber } from "@/lib/useStored";

const ACTIVE = ["queued", "running", "cancelling"];

type View = { key: string; exp?: Experiment; result?: CompareResult; error?: ApiError; empty?: boolean };

export default function ComparePage() {
  const router = useRouter();
  const params = useSearchParams();
  const experimentParam = params.get("experiment");
  const runsParam = params.get("runs");
  const refParam = params.get("ref") ?? undefined;

  const [sourceChoice, setSourceChoice] = useState<"lab" | "app" | null>(null);
  const source = sourceChoice ?? (runsParam ? "app" : "lab");
  const [presets, setPresets] = useState<Preset[]>([]);
  const [picked, setPicked] = useState<{ selection: string[]; reference: string | null } | null>(null);
  const storedTrials = useStoredNumber("vx-trials", 5);
  const storedDuration = useStoredNumber("vx-duration", 10);
  const [trialsEdit, setTrialsEdit] = useState<number | null>(null);
  const [durationEdit, setDurationEdit] = useState<number | null>(null);
  const trials = trialsEdit ?? storedTrials;
  const durationS = durationEdit ?? storedDuration;
  const [reloadKey, setReloadKey] = useState(0);
  const [view, setView] = useState<View>({ key: "" });
  const [ts, setTs] = useState<{ id: string; data: Timeseries | null } | null>(null);
  const [actionError, setActionError] = useState<ApiError | null>(null);

  const key = `${experimentParam}|${runsParam}|${refParam}|${reloadKey}`;
  const loading = view.key !== key;

  useEffect(() => {
    api.presets().then(setPresets).catch(() => undefined); // an unreachable service surfaces via the main loader
  }, []);

  // Load whatever the URL points at. State is only set from async callbacks; the experiment is polled while it runs.
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const done = (v: Omit<View, "key">) => { if (!cancelled) setView({ key, ...v }); };
    const fail = (e: unknown) => {
      let err = e instanceof ApiError ? e : new ApiError(0, String(e), "");
      if (err.status === 404) {  // API wording ("GET /v2/...") -> what to do in the UI
        err = new ApiError(404, err.message, "Open Runs to pick an existing comparison, or start a new one above.");
      }
      done({ error: err });
    };

    const loadExperiment = async (id: string): Promise<void> => {
      const e = await api.experiment(id);
      if (cancelled) return;
      if (ACTIVE.includes(e.status)) {
        done({ exp: e });
        timer = setTimeout(() => loadExperiment(id).catch(fail), 1000);
        return;
      }
      if (e.status !== "complete" && e.progress.done === 0) {
        done({ exp: e, error: new ApiError(0, e.error ?? `The experiment was ${e.status} before any trial finished.`, "Pick algorithms and run it again.") });
        return;
      }
      const r = await api.compareExperiment(id, refParam);
      done({ exp: e, result: r });
    };

    if (experimentParam) loadExperiment(experimentParam).catch(fail);
    else if (runsParam) api.compareRuns(runsParam.split(","), refParam).then((r) => done({ result: r })).catch(fail);
    else {
      api.experiments("?source=lab&limit=1").then((latest) => {
        if (cancelled) return;
        if (latest.items.length) router.replace(`/?experiment=${latest.items[0].experiment_id}`);
        else done({ empty: true });
      }).catch(fail);
    }
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [key, experimentParam, runsParam, refParam, router]);

  // Machine view data for a finished Lab experiment.
  const tsFor = view.result?.source === "lab" ? view.result.experiment?.experiment_id : undefined;
  useEffect(() => {
    if (!tsFor) return;
    let cancelled = false;
    api.timeseries(tsFor).then((d) => !cancelled && setTs({ id: tsFor, data: d }))
      .catch(() => !cancelled && setTs({ id: tsFor, data: null }));
    return () => { cancelled = true; };
  }, [tsFor]);

  const exp = view.exp;
  const running = !!exp && ACTIVE.includes(exp.status);
  // The picker shows the user's own picks; otherwise it mirrors the experiment on screen.
  const selection = picked?.selection ?? exp?.params.presets ?? [];
  const reference = picked?.reference ?? (picked ? null : refParam ?? exp?.reference_preset ?? null);

  const start = async (sel: string[], ref: string | null) => {
    setActionError(null);
    try {
      const out = await api.startLab({ presets: sel, reference: ref ?? sel[0], trials, duration_s: durationS });
      setPicked(null);
      router.push(`/?experiment=${out.experiment_id}`);
    } catch (e) {
      setActionError(e as ApiError);
    }
  };

  const error = actionError ?? view.error;
  const result = !loading ? view.result : undefined;

  return (
    <div className="page">
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 12 }}>
        <div>
          <h1>Compare</h1>
          <p className="muted" style={{ margin: 0 }}>What changes if you replace one crypto algorithm with another?</p>
        </div>
        <div className="segmented" role="group" aria-label="Data source">
          <button aria-pressed={source === "lab"} onClick={() => setSourceChoice("lab")}>Lab test</button>
          <button aria-pressed={source === "app"} onClick={() => setSourceChoice("app")}>My app</button>
        </div>
      </div>

      {source === "lab" ? (
        <AlgorithmPicker presets={presets} selection={selection} reference={reference} trials={trials} durationS={durationS}
                         running={running}
                         onSelection={(sel, ref) => setPicked({ selection: sel, reference: ref ?? (reference && sel.includes(reference) ? reference : sel[0] ?? null) })}
                         onReference={(ref) => setPicked({ selection, reference: ref })}
                         onTrials={(n) => setTrialsEdit(Math.min(20, Math.max(1, n || 1)))}
                         onDuration={(s) => setDurationEdit(Math.min(60, Math.max(1, s || 1)))}
                         onRun={() => start(selection, reference)}
                         onQuickRun={(sel) => { setPicked({ selection: sel, reference: sel[0] }); start(sel, sel[0]); }} />
      ) : (
        <AppRunsPicker onCompare={(ids) => router.push(`/?runs=${ids.join(",")}`)} />
      )}

      {error ? (
        <ErrorState error={error.message} fix={error.fix} onRetry={() => { setActionError(null); setReloadKey((k) => k + 1); }} />
      ) : running && exp ? (
        <LabProgress exp={exp} onCancel={() => api.cancel(exp.experiment_id)
          .then(() => setView((v) => (v.exp ? { ...v, exp: { ...v.exp, status: "cancelling" } } : v)))
          .catch(setActionError)} />
      ) : loading ? (
        <Skeleton />
      ) : result ? (
        <ResultView result={result} ts={ts?.id === tsFor ? ts?.data ?? null : null} tsLoading={!!tsFor && ts?.id !== tsFor}
                    onReference={(k) => router.replace(result.source === "lab" && result.experiment
                      ? `/?experiment=${result.experiment.experiment_id}&ref=${encodeURIComponent(k)}`
                      : `/?runs=${runsParam}&ref=${encodeURIComponent(k)}`)} />
      ) : view.empty && source === "lab" ? (
        <EmptyState onPick={(sel) => { setPicked({ selection: sel, reference: sel[0] }); start(sel, sel[0]); }} />
      ) : null}
    </div>
  );
}
