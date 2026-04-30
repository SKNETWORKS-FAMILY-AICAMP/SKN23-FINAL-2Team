import React, { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import {
  AlertTriangle,
  Layers,
  Save,
  X,
  CheckCircle2,
} from "lucide-react";

import {
  fetchPipingStandardTerms,
  saveLayerMappings,
  type LayerMappingRow,
  type StandardTermRow,
} from "../../api/cadApi";

const C = {
  bg: "#020817",
  panel: "#07111f",
  border: "rgba(71,85,105,0.5)",
  text: "#e2e8f0",
  sub: "#64748b",
  accent: "#0ea5e9",
};

type Props = {
  open: boolean;
  onClose: () => void;
  layerNames: string[];
  onSaved: () => void;
};

export default function UnmappedLayersModal({
  open,
  onClose,
  layerNames,
  onSaved,
}: Props) {
  const [terms, setTerms] = useState<StandardTermRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [choices, setChoices] = useState<Record<string, string>>({});
  const [roles, setRoles] = useState<Record<string, string>>({});

  useEffect(() => {
    if (!open) return;

    setLoading(true);
    setError(null);
    setChoices({});
    setRoles({});

    fetchPipingStandardTerms()
      .then((d) => setTerms(d.terms || []))
      .catch((e) => setError(axiosErrorMessage(e)))
      .finally(() => setLoading(false));
  }, [open, layerNames.join(",")]);

  if (!open) return null;

  const handleSave = async () => {
    const orgId = localStorage.getItem("skn23_org_id") || "";

    if (!orgId) {
      setError("조직 ID가 없습니다.");
      return;
    }

    const mappings = layerNames
      .map((name) => {
        const id = choices[name];
        if (!id) return null;

        return {
          source_key: name,
          standard_term_id: id,
          layer_role: roles[name] || undefined,
        };
      })
      .filter(Boolean) as LayerMappingRow[];

    if (mappings.length === 0) {
      setError("최소 1개 이상 선택하세요.");
      return;
    }

    setSaving(true);

    try {
      await saveLayerMappings(orgId, mappings);
      onSaved();
      onClose();
    } catch (e) {
      setError(axiosErrorMessage(e));
    } finally {
      setSaving(false);
    }
  };

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center"
      style={{ background: "rgba(0,0,0,0.65)" }}
    >
      <div
        className="w-full max-w-2xl rounded-2xl border shadow-2xl"
        style={{
          background: C.panel,
          borderColor: C.border,
        }}
      >
        {/* HEADER */}
        <header className="flex items-center justify-between px-6 py-4 border-b border-slate-800">
          <div className="flex items-center gap-3">
            <Layers className="text-blue-400" />
            <div>
              <h2 className="text-sm font-semibold text-white">
                레이어 매핑 필요
              </h2>
              <p className="text-xs text-slate-400">
                분석 전에 표준 용어 연결이 필요합니다
              </p>
            </div>
          </div>

          <button onClick={onClose}>
            <X className="text-slate-500" />
          </button>
        </header>

        {/* BODY */}
        <div className="px-6 py-4 max-h-[420px] overflow-y-auto space-y-4">
          {/* ALERT */}
          <div className="flex items-center gap-3 rounded-xl bg-red-500/10 border border-red-500/20 px-4 py-3">
            <AlertTriangle className="text-red-400" />
            <p className="text-sm text-red-300">
              {layerNames.length}개의 레이어가 미등록 상태입니다
            </p>
          </div>

          {loading && (
            <p className="text-sm text-slate-400">
              표준 용어 불러오는 중...
            </p>
          )}

          {error && (
            <p className="text-sm text-red-400">{error}</p>
          )}

          {!loading &&
            layerNames.map((name) => (
              <div
                key={name}
                className="rounded-xl border border-slate-700 p-4 bg-slate-900/50"
              >
                <p className="text-xs text-slate-400 mb-2">{name}</p>

                <select
                  className="w-full mb-3 rounded-lg border px-3 py-2 bg-slate-950 text-white text-sm"
                  value={choices[name] || ""}
                  onChange={(e) =>
                    setChoices((p) => ({ ...p, [name]: e.target.value }))
                  }
                >
                  <option value="">표준 용어 선택</option>
                  {terms.map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.standard_name}
                    </option>
                  ))}
                </select>

                <select
                  className="w-full rounded-lg border px-3 py-2 bg-slate-950 text-white text-sm"
                  value={roles[name] || ""}
                  onChange={(e) =>
                    setRoles((p) => ({ ...p, [name]: e.target.value }))
                  }
                >
                  <option value="">레이어 역할 자동</option>
                  <option value="arch">건축</option>
                  <option value="mep">설비</option>
                  <option value="aux">보조</option>
                </select>
              </div>
            ))}
        </div>

        {/* FOOTER */}
        <footer className="flex justify-between items-center px-6 py-4 border-t border-slate-800">
          <button
            onClick={onClose}
            className="text-sm text-slate-400"
          >
            닫기
          </button>

          <button
            onClick={handleSave}
            disabled={saving}
            className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-semibold text-white bg-blue-600 hover:bg-blue-500"
          >
            {saving ? "저장 중..." : "선택 저장"}
            <Save className="w-4 h-4" />
          </button>
        </footer>
      </div>
    </div>,
    document.body
  );
}

function axiosErrorMessage(e: unknown): string {
  if (e instanceof Error) return e.message;
  return "요청 실패";
}