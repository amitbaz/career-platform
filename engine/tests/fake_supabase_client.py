class FakeSupabaseClient:
    """In-memory stand-in for SupabaseClient, scoped to one fake user's rows."""

    def __init__(self):
        self.user_id = "u1"
        self.rows = {"job_hunter_search_profiles": [], "job_hunter_search_profile_markets": []}
        self._next_id = 1

    def _new_id(self) -> str:
        value = f"id-{self._next_id}"
        self._next_id += 1
        return value

    def select(self, table, *, params=None):
        params = params or {}
        rows = self.rows[table]
        if "user_id" in params:
            rows = [r for r in rows if r["user_id"] == params["user_id"].removeprefix("eq.")]
        if "profile_id" in params:
            rows = [r for r in rows if r["profile_id"] == params["profile_id"].removeprefix("eq.")]
        if "order" in params:
            column, _, direction = params["order"].partition(".")
            rows = sorted(rows, key=lambda r: r[column], reverse=direction == "desc")
        return rows[: int(params["limit"])] if "limit" in params else rows

    def upsert(self, table, rows, *, on_conflict):
        # Only the profiles table has a real uniqueness constraint (one
        # profile per user) worth simulating here -- deduping the markets
        # table too would silently collapse rows that a caller inserted
        # twice in the same batch, hiding bugs a real Postgres upsert would
        # surface differently (and that config.py's own duplicate-market-id
        # validation is exercised against downstream of the store).
        written = []
        for row in rows:
            row = dict(row)
            row.setdefault("id", self._new_id())
            if table == "job_hunter_search_profiles":
                existing = [
                    r for r in self.rows[table]
                    if all(r.get(k) == row.get(k) for k in on_conflict.split(","))
                ]
                if existing:
                    existing[0].update(row)
                    written.append(existing[0])
                    continue
            self.rows[table].append(row)
            written.append(row)
        return written

    def delete(self, table, *, params):
        key = params["profile_id"].removeprefix("eq.")
        self.rows[table] = [r for r in self.rows[table] if r.get("profile_id") != key]
