// Labels a code path for the profiler, so the compare view matches exactly this work across variants.
// Without the profiler loaded (or not initialised) it simply runs fn.
export async function scope<T>(name: string, fn: () => Promise<T>): Promise<T> {
  try {
    const { span } = await import("@vayunx/profiler");
    return await span(name, fn);
  } catch (e) {
    if (e instanceof Error && /Cannot find module/.test(e.message)) return fn();
    throw e;
  }
}
