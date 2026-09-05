import path from "node:path";
import { fileURLToPath } from "node:url";

// Supabase migrations are owned by the monorepo, not by Relay, so they live at
// <repo root>/supabase rather than inside apps/relay.
const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../..");

export function supabaseMigration(fileName: string): string {
  return path.join(repoRoot, "supabase", "migrations", fileName);
}
