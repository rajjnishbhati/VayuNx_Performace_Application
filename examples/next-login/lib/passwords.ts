import { createHash, timingSafeEqual } from "node:crypto";

import argon2 from "argon2";

/** md5 | argon2id, from VAYUNX_VARIANT (read at startup). */
export const VARIANT = (process.env.VAYUNX_VARIANT ?? "argon2id").trim().toLowerCase();

if (VARIANT !== "md5" && VARIANT !== "argon2id") {
  throw new Error(`VAYUNX_VARIANT must be 'md5' or 'argon2id', got '${VARIANT}'`);
}

// OWASP Password Storage minimum for Argon2id: m=19456 KiB, t=2, p=1
const ARGON2_OPTIONS = { type: argon2.argon2id, memoryCost: 19456, timeCost: 2, parallelism: 1 } as const;

export async function hashPassword(password: string): Promise<string> {
  if (VARIANT === "md5") return createHash("md5").update(password).digest("hex"); // INSECURE: demo only
  return argon2.hash(password, ARGON2_OPTIONS);
}

export async function verifyPassword(stored: string, password: string): Promise<boolean> {
  if (VARIANT === "md5") {
    const a = Buffer.from(stored, "hex");
    const b = createHash("md5").update(password).digest();
    return a.length === b.length && timingSafeEqual(a, b);
  }
  try {
    return await argon2.verify(stored, password);
  } catch {
    return false;
  }
}
