/*
 * File: frontend/src/api/cadApi.ts
 * Description: CAD interop(레이어 매핑, 표준 용어 조회) REST 호출
 */

import axios from "axios";
import { getAgentApiHeaders } from "../utils/documentApiAuth";

const CAD_BASE =
  ((import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ||
    "http://localhost:8000") + "/api/v1/cad";

export type StandardTermRow = {
  id: string;
  category?: string | null;
  standard_name: string;
  aliases?: unknown;
};

export async function fetchPipingStandardTerms(): Promise<{ terms: StandardTermRow[]; total: number }> {
  const { data } = await axios.get<{
    terms: StandardTermRow[];
    total: number;
  }>(`${CAD_BASE}/debug/standard-terms`, {
    params: { domain: "pipe" },
    headers: { ...getAgentApiHeaders() },
  });
  return data;
}

export type LayerMappingRow = {
  source_key: string;
  standard_term_id: string;
  /** arch | mep | aux — 휴리스틱 대신 DB layer_role(선택) */
  layer_role?: string;
};

export async function saveLayerMappings(
  orgId: string,
  mappings: LayerMappingRow[]
): Promise<void> {
  if (!mappings.length) return;
  await axios.post(`${CAD_BASE}/mapping-rules/layer-batch`, {
    org_id: orgId,
    domain: "pipe",
    mappings,
  }, {
    headers: { ...getAgentApiHeaders() },
  });
}
