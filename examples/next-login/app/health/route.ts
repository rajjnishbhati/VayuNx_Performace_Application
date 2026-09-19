import { VARIANT } from "../../lib/passwords";
import { users } from "../../lib/users";

export const dynamic = "force-dynamic";

export function GET() {
  return Response.json({ ok: true, variant: VARIANT, users: users.size });
}
