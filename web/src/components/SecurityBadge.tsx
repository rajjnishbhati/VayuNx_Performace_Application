import type { Security } from "@/lib/types";

/** The security question differs by algorithm: a password hash is judged on password fitness, a key
 *  agreement or signature on whether it survives a quantum computer. Asking the wrong one produces
 *  nonsense like "ML-KEM-768: not for passwords" in red, so the badge follows the algorithm. */
export default function SecurityBadge({ security }: { security: Security | null | undefined }) {
  if (!security) return <span className="badge neutral">? Unknown</span>;
  if (security.quantum === "post-quantum") {
    return <span className="badge good"><span aria-hidden>✓</span> Quantum-safe</span>;
  }
  if (security.quantum === "classical") {
    return <span className="badge warning"><span aria-hidden>⚠</span> Classical</span>;
  }
  return security.safe_for_passwords
    ? <span className="badge good"><span aria-hidden>✓</span> Safe for passwords</span>
    : <span className="badge critical"><span aria-hidden>✕</span> Not for passwords</span>;
}
