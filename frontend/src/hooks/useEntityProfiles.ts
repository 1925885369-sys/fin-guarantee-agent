import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { EntityProfileSummary, EntityProfileStatus } from "../types";

export function useEntityProfiles() {
  const [profiles, setProfiles] = useState<EntityProfileSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const load = useCallback(async (search = "", status: EntityProfileStatus | "all" = "all") => {
    setLoading(true); setError("");
    try { setProfiles(await api.entityProfiles({ search, status })); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "主体档案读取失败"); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  return { profiles, loading, error, load };
}
