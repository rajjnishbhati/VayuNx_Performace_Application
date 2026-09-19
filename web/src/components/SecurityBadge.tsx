import type { Security } from "@/lib/types";

/** Status colour + icon + label, never colour alone. Only used for security state. */
export default function SecurityBadge({ security }: { security: Security | null | undefined }) {
  if (!security) return <span className="badge neutral">? Unknown</span>;
  return security.safe_for_passwords
    ? <span className="badge good"><span aria-hidden>✓</span> Safe for passwords</span>
    : <span className="badge critical"><span aria-hidden>✕</span> Not for passwords</span>;
}
