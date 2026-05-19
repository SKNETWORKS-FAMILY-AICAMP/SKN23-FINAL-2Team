/*
 * File    : frontend/src/components/spec/SpecManagerModal.tsx
 * Author      : 김지우
 * Create      : 2026-04-06
 * Description : 시방서 관리 모달 (보관기간·도메인 셀렉트, 업로드 진행/완료/실패 모달)
 *
 * Modification History :
 *     - 2026-04-06 (김지우) : 초기 UI 구현
 *     - 2026-04-21 (김지우) : 더미 제거 + 실제 API 연동 + 업로드 진행/완료/실패 모달 + 기간연장
 *     - 2026-04-21 (김지우) : 보관기간(1~12개월)·도메인 셀렉트 동일 레이아웃
 *     - 2026-04-21 (김지우) : 커스텀 드롭다운(아래 펼침·스크롤) + 도메인/보관기간 순서
 *     - 2026-04-21 (김지우) : 시방서 관리 — 연장을 행 버튼 대신 선택+툴바 연장 모달
 */

import React, { useState, useEffect, useLayoutEffect, useRef } from "react";
import { createPortal } from "react-dom";
import {
  Upload,
  Trash2,
  FolderOpen,
  Search,
  X,
  AlertCircle,
  CheckCircle,
  Loader2,
  Clock,
  ChevronDown,
} from "lucide-react";
import { useAgentStore } from "../../store/agentStore";
import { getDocumentApiHeaders, isAgentApiRegistered } from "../../utils/documentApiAuth";

const T = {
  bg: "#1e1e1e",
  panelBg: "#252526",
  cardBg: "#2d2d2d",
  inputBg: "#3c3c3c",
  border: "#3c3c3c",
  borderLight: "#4a4a4a",
  text: "#cccccc",
  textSub: "#999999",
  textMuted: "#666666",
  accent: "#0078d4",
  accentHover: "#1a8ae8",
  hover: "#2a2d2e",
  selected: "#094771",
  danger: "#f14c4c",
  dangerHover: "#d73a3a",
  white: "#e8e8e8",
  success: "#4EC9B0",
} as const;

const API_BASE =
  ((import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ||
    "http://localhost:8000") + "/api/v1/documents";

const DOMAIN_OPTIONS = [
  { value: "elec", label: "전기" },
  { value: "pipe", label: "배관" },
  { value: "arch", label: "건축" },
  { value: "fire", label: "소방" },
] as const;

const RETENTION_OPTIONS = [
  { value: 1, label: "1개월" },
  { value: 3, label: "3개월" },
  { value: 6, label: "6개월" },
  { value: 12, label: "12개월" },
] as const;

const EXTEND_OPTIONS = [
  { value: 1, label: "+1개월" },
  { value: 3, label: "+3개월" },
  { value: 6, label: "+6개월" },
] as const;

/** 네이티브 select는 뷰포트에 따라 위로 펼쳐짐 → 고정 위치 + 스크롤 리스트로 아래 펼침 */
function CustomSelect({
  value,
  placeholder,
  options,
  onChange,
}: {
  value: string;
  placeholder: string;
  options: readonly { value: string; label: string }[];
  onChange: (next: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [menuStyle, setMenuStyle] = useState<React.CSSProperties>({});

  const syncMenuPosition = () => {
    const el = wrapRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const gap = 4;
    const desiredMax = 220;
    const spaceBelow = window.innerHeight - r.bottom - gap - 8;
    const maxHeight = Math.max(100, Math.min(desiredMax, spaceBelow));
    setMenuStyle({
      position: "fixed",
      top: r.bottom + gap,
      left: r.left,
      width: r.width,
      maxHeight,
      overflowY: "auto",
      zIndex: 5000,
      background: T.cardBg,
      border: `1px solid ${T.borderLight}`,
      borderRadius: "4px",
      boxShadow: "0 8px 24px rgba(0,0,0,0.45)",
      padding: "4px",
    });
  };

  useLayoutEffect(() => {
    if (!open) return;
    syncMenuPosition();
    const onReposition = () => syncMenuPosition();
    window.addEventListener("scroll", onReposition, true);
    window.addEventListener("resize", onReposition);
    return () => {
      window.removeEventListener("scroll", onReposition, true);
      window.removeEventListener("resize", onReposition);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (wrapRef.current?.contains(t)) return;
      if (menuRef.current?.contains(t)) return;
      setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const selected = options.find((o) => o.value === value);
  const displayText = value === "" || !selected ? placeholder : selected.label;
  const isPlaceholder = value === "" || !selected;

  const triggerStyle: React.CSSProperties = {
    width: "100%",
    height: "38px",
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    gap: "8px",
    padding: "0 12px",
    background: T.inputBg,
    color: isPlaceholder ? T.textMuted : T.white,
    border: `1px solid ${T.border}`,
    borderRadius: "4px",
    outline: "none",
    fontSize: "13px",
    cursor: "pointer",
    textAlign: "left",
  };

  const optionBtn = (active: boolean): React.CSSProperties => ({
    display: "block",
    width: "100%",
    padding: "8px 10px",
    borderRadius: "4px",
    border: "none",
    cursor: "pointer",
    textAlign: "left",
    fontSize: "13px",
    background: active ? T.selected : "transparent",
    color: active ? T.white : T.text,
  });

  return (
    <div ref={wrapRef} style={{ position: "relative" }}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        style={triggerStyle}
      >
        <span
          style={{
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {displayText}
        </span>
        <ChevronDown
          size={16}
          style={{
            flexShrink: 0,
            color: T.textMuted,
            transform: open ? "rotate(180deg)" : "rotate(0deg)",
            transition: "transform 0.15s ease",
          }}
        />
      </button>
      {open &&
        createPortal(
          <div ref={menuRef} style={menuStyle}>
            {options.map((opt) => (
              <button
                key={opt.value}
                type="button"
                onClick={() => {
                  onChange(opt.value);
                  setOpen(false);
                }}
                style={optionBtn(value === opt.value)}
              >
                {opt.label}
              </button>
            ))}
          </div>,
          document.body,
        )}
    </div>
  );
}

interface TempDoc {
  id: string;
  file_name: string;
  comment: string | null;
  status: string;
  domain?: string | null;
  reg_date: string;
  delete_date: string;
  storage_path?: string | null;
}

interface ProjectSpecLinkPayload {
  project_id?: string;
  org_id?: string;
  documents?: { temp_document_id?: string }[];
}

const PROJECT_ID_STORAGE_KEY = "skn23_current_project_id";

function createProjectId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `00000000-0000-4000-8000-${Date.now().toString(16).padStart(12, "0").slice(-12)}`;
}

function getStoredProjectId(): string {
  return localStorage.getItem(PROJECT_ID_STORAGE_KEY) || "";
}

function storeProjectId(projectId: string) {
  if (projectId) localStorage.setItem(PROJECT_ID_STORAGE_KEY, projectId);
}

function buildUploadSuccessMessage(data: any): string {
  const lines = [
    `파일: ${data.filename ?? "-"}`,
    `분석된 청크 수: ${data.chunk_count ?? 0}개`,
  ];

  if (typeof data.db_inserted_chunks === "number") {
    lines.push(`DB 저장 청크 수: ${data.db_inserted_chunks}개`);
  }

  const stats = data.table_markdown_stats;
  if (stats && typeof stats === "object") {
    const tableChunks = Number(stats.table_chunks ?? 0);
    const filled = Number(stats.table_markdown_filled ?? 0);
    lines.push(`표 복원: ${filled}/${tableChunks}개`);
  }

  return lines.join("\n");
}

/** C# 숨김 폴더 link.json 동기화 (참조 메타만) */
function postTempSpecLinkToHost(
  projectId: string,
  trackedIds: string[],
  rows: TempDoc[],
  org: string,
) {
  const docs = trackedIds.map((id) => {
    const row = rows.find((s) => s.id === id);
    return {
      temp_document_id: id,
      storage_path: row?.storage_path ?? "",
      file_name: row?.file_name ?? "",
    };
  });
  const payload = {
    version: 2,
    project_id: projectId,
    org_id: org,
    updated_at: new Date().toISOString(),
    documents: docs,
  };
  try {
    window.chrome?.webview?.postMessage(
      JSON.stringify({ action: "TEMP_SPEC_SELECTION", payload }),
    );
  } catch {
    /* noop */
  }
}

function postTempSpecLinkClearToHost() {
  try {
    window.chrome?.webview?.postMessage(
      JSON.stringify({ action: "TEMP_SPEC_LINK_CLEAR_CURRENT" }),
    );
  } catch {
    /* noop */
  }
}

async function fetchProjectSpecLinks(projectId: string, org: string): Promise<TempDoc[]> {
  if (!projectId || !org) return [];
  const res = await fetch(
    `${API_BASE}/temp/projects/${encodeURIComponent(projectId)}/specs?org_id=${encodeURIComponent(org)}`,
    { headers: { ...getDocumentApiHeaders() } },
  );
  if (!res.ok) return [];
  const data = await res.json();
  return (data.documents || []).map((d: any) => ({
    id: d.temp_document_id,
    file_name: d.file_name ?? "",
    comment: d.comment ?? null,
    status: d.status ?? "",
    domain: d.domain ?? null,
    reg_date: d.reg_date ?? new Date().toISOString(),
    delete_date: d.delete_date ?? "",
    storage_path: d.storage_path ?? "",
  }));
}

async function saveProjectSpecLinks(
  projectId: string,
  org: string,
  ids: string[],
): Promise<TempDoc[]> {
  const res = await fetch(
    `${API_BASE}/temp/projects/${encodeURIComponent(projectId)}/specs`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json", ...getDocumentApiHeaders() },
      body: JSON.stringify({ org_id: org, temp_document_ids: ids }),
    },
  );
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.status !== "success") {
    throw new Error(data.detail || "Failed to save project spec links");
  }
  return (data.documents || []).map((d: any) => ({
    id: d.temp_document_id,
    file_name: d.file_name ?? "",
    comment: d.comment ?? null,
    status: d.status ?? "",
    domain: d.domain ?? null,
    reg_date: d.reg_date ?? new Date().toISOString(),
    delete_date: d.delete_date ?? "",
    storage_path: d.storage_path ?? "",
  }));
}

interface Props {
  isOpen: boolean;
  onClose: () => void;
}

export default function SpecManagerModal({ isOpen, onClose }: Props) {
  const { setSelectedTempSpecIds, selectedTempSpecIds } = useAgentStore();
  const [activeTab, setActiveTab] = useState<"upload" | "manage">("upload");

  // 업로드 폼
  const [files, setFiles] = useState<File[]>([]);
  const [comment, setComment] = useState("");
  const [domain, setDomain] = useState<string | null>(null);
  const [retentionMonths, setRetentionMonths] = useState<number | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // 업로드 결과 모달
  const [uploadResult, setUploadResult] = useState<{
    type: "success" | "error";
    title: string;
    message: string;
  } | null>(null);

  // 관리 탭
  const [specList, setSpecList] = useState<TempDoc[]>([]);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [projectId, setProjectId] = useState<string>(() => getStoredProjectId());
  const [projectNotice, setProjectNotice] = useState("");
  const [isApplyingProject, setIsApplyingProject] = useState(false);

  // 기간연장 모달 (체크 선택 후 툴바 «연장»)
  const [showExtendModal, setShowExtendModal] = useState(false);
  const [extendMonths, setExtendMonths] = useState(1);

  // API 키 가드
  const isApiKeyRegistered = !!localStorage.getItem("skn23_api_key_registered");

  const orgId = localStorage.getItem("skn23_org_id") || "";
  const deviceId = localStorage.getItem("skn23_device_id") || "";

  const ensureProjectId = () => {
    const existing = projectId || getStoredProjectId();
    if (existing) return existing;
    const created = createProjectId();
    storeProjectId(created);
    setProjectId(created);
    return created;
  };

  const mergeSpecRows = (rows: TempDoc[]) => {
    if (rows.length === 0) return;
    setSpecList((prev) => {
      const byId = new Map(prev.map((row) => [row.id, row]));
      for (const row of rows) byId.set(row.id, { ...byId.get(row.id), ...row });
      return Array.from(byId.values());
    });
  };

  const publishProjectSpecsToHost = (pid: string, ids: string[], rows: TempDoc[]) => {
    postTempSpecLinkToHost(pid, ids, rows, orgId);
  };

  const clearProjectSpecsFromHost = () => {
    localStorage.removeItem(PROJECT_ID_STORAGE_KEY);
    setProjectId("");
    postTempSpecLinkClearToHost();
  };

  const applyProjectSpecsFromRows = (rows: TempDoc[]) => {
    const ids = rows.map((row) => row.id).filter(Boolean);
    setSelectedTempSpecIds(ids);
    setSelectedIds(ids);
    mergeSpecRows(rows);
  };

  const saveSelectedSpecsToProject = async () => {
    if (!isAgentApiRegistered() || !orgId) {
      setProjectNotice("API 키 등록 후 프로젝트 시방서를 사용할 수 있습니다.");
      return;
    }
    const pid = selectedIds.length > 0 ? ensureProjectId() : projectId || getStoredProjectId();
    setIsApplyingProject(true);
    setProjectNotice("");
    try {
      if (selectedIds.length === 0) {
        if (pid) {
          await saveProjectSpecLinks(pid, orgId, []);
        }
        setSelectedTempSpecIds([]);
        setSelectedIds([]);
        clearProjectSpecsFromHost();
        setProjectNotice("?꾨줈?앺듃 ?쒕갑???곸슜???댁젣?덉뒿?덈떎.");
        return;
      }
      const rows = await saveProjectSpecLinks(pid, orgId, selectedIds);
      applyProjectSpecsFromRows(rows);
      publishProjectSpecsToHost(pid, rows.map((row) => row.id).filter(Boolean), rows);
      setProjectNotice(`프로젝트 시방서 ${rows.length}개를 적용했습니다.`);
    } catch (err: any) {
      setProjectNotice(err?.message || "프로젝트 시방서 적용에 실패했습니다.");
    } finally {
      setIsApplyingProject(false);
    }
  };

  // 목록 조회
  const fetchList = async () => {
    if (!orgId) return;
    setIsLoading(true);
    try {
      const res = await fetch(
        `${API_BASE}/temp?org_id=${encodeURIComponent(orgId)}`,
        {
          headers: { ...getDocumentApiHeaders() },
        },
      );
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "목록 조회 실패");
      const docs = data.documents ?? [];
      setSpecList(docs);
      const tracked = useAgentStore.getState().selectedTempSpecIds;
      if (tracked.length > 0 && window.chrome?.webview) {
        const pid = projectId || getStoredProjectId() || ensureProjectId();
        publishProjectSpecsToHost(pid, tracked, docs);
      }
    } catch {
      setSpecList([]);
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    if (activeTab === "manage") fetchList();
  }, [activeTab]);

  useEffect(() => {
    try {
      window.chrome?.webview?.postMessage(JSON.stringify({ action: "SPEC_MODAL_READY" }));
    } catch {
      /* noop */
    }

    const handleHostMessage = (event: Event) => {
      let msg: any;
      try {
        msg = JSON.parse((event as any).data);
      } catch {
        return;
      }
      if (msg.action === "TEMP_SPEC_LINK_CLEARED") {
        localStorage.removeItem(PROJECT_ID_STORAGE_KEY);
        setProjectId("");
        setSelectedTempSpecIds([]);
        setSelectedIds([]);
        setProjectNotice("");
        return;
      }
      if (msg.action !== "TEMP_SPEC_LINK_LOADED") return;

      const payload = (msg.payload || {}) as ProjectSpecLinkPayload;
      const payloadOrg = payload.org_id || "";
      if (payloadOrg && orgId && payloadOrg !== orgId) return;

      if (!isAgentApiRegistered() || !orgId) {
        localStorage.removeItem(PROJECT_ID_STORAGE_KEY);
        setProjectId("");
        setSelectedTempSpecIds([]);
        setSelectedIds([]);
        setProjectNotice("API 키 등록 후 sidecar 시방서를 사용할 수 있습니다.");
        return;
      }

      const pid = payload.project_id || getStoredProjectId();
      if (pid) {
        storeProjectId(pid);
        setProjectId(pid);
      }

      const applyPayloadDocuments = () => {
        const ids = (payload.documents || [])
          .map((doc) => doc.temp_document_id)
          .filter((id): id is string => !!id);
        if (ids.length > 0) {
          setSelectedTempSpecIds(ids);
          setSelectedIds(ids);
        }
      };

      if (pid) {
        void fetchProjectSpecLinks(pid, orgId).then((rows) => {
          if (rows.length > 0) {
            applyProjectSpecsFromRows(rows);
            setProjectNotice(`sidecar 프로젝트 시방서 ${rows.length}개를 불러왔습니다.`);
            return;
          }
          applyPayloadDocuments();
        });
      } else {
        applyPayloadDocuments();
      }
    };

    window.chrome?.webview?.addEventListener("message", handleHostMessage);
    return () => {
      window.chrome?.webview?.removeEventListener("message", handleHostMessage);
    };
  }, [orgId]);

  // 업로드 핸들러 — 실제 API
  const handleUpload = async () => {
    if (files.length === 0) return;
    if (!orgId || !deviceId) {
      setUploadResult({
        type: "error",
        title: "인증 정보 확인",
        message: "조직 또는 장치 정보가 없습니다. API 키를 다시 등록한 뒤 시방서를 업로드해 주세요.",
      });
      return;
    }
    if (domain == null || retentionMonths == null) {
      setUploadResult({
        type: "error",
        title: "입력 확인",
        message: "파일 보관 기간과 도메인을 선택해주세요.",
      });
      return;
    }
    setIsUploading(true);
    setUploadResult(null);

    try {
      const file = files[0];
      const form = new FormData();
      form.append("file", file);
      form.append("org_id", orgId);
      form.append("device_id", deviceId);
      form.append("domain_type", domain);
      form.append("category", "spec");
      form.append("comment", comment);
      form.append("retention_months", String(retentionMonths));

      const res = await fetch(`${API_BASE}/upload/temp`, {
        method: "POST",
        headers: { ...getDocumentApiHeaders() },
        body: form,
      });
      const data = await res.json();

      if (!res.ok || data.status !== "success") {
        throw new Error(data.detail || "업로드 실패");
      }

      setUploadResult({
        type: "success",
        title: "시방서 등록 완료",
        message: buildUploadSuccessMessage(data),
      });
      if (data.document_id) {
        const pid = ensureProjectId();
        const current = useAgentStore.getState().selectedTempSpecIds;
        const next = [...current, data.document_id];
        setSelectedTempSpecIds(next);
        const extraRow: TempDoc = {
          id: data.document_id,
          file_name: data.filename ?? "",
          comment: null,
          status: "completed",
          domain,
          reg_date: new Date().toISOString(),
          delete_date: "",
          storage_path: "",
        };
        try {
          await saveProjectSpecLinks(pid, orgId, next);
          setProjectNotice("업로드한 시방서를 현재 프로젝트에 적용했습니다.");
        } catch {
          setProjectNotice("업로드는 완료됐지만 프로젝트 링크 저장은 실패했습니다.");
        }
        publishProjectSpecsToHost(pid, next, [...specList, extraRow]);
      }
      setFiles([]);
      setComment("");
      setDomain(null);
      setRetentionMonths(null);
    } catch (err: any) {
      setUploadResult({
        type: "error",
        title: "시방서 등록 실패",
        message: err.message || "알 수 없는 오류가 발생했습니다.",
      });
    } finally {
      setIsUploading(false);
    }
  };

  // 삭제
  const executeDelete = async () => {
    setShowDeleteConfirm(false);
    const q = `org_id=${encodeURIComponent(orgId)}`;
    const remaining = selectedTempSpecIds.filter(
      (x) => !selectedIds.includes(x),
    );
    const remainingRows = specList.filter((row) => remaining.includes(row.id));
    setSelectedTempSpecIds(remaining);
    for (const id of selectedIds) {
      try {
        await fetch(`${API_BASE}/temp/${id}?${q}`, {
          method: "DELETE",
          headers: { ...getDocumentApiHeaders() },
        });
      } catch {
        /* swallow */
      }
    }
    const pid = projectId || getStoredProjectId();
    if (pid && orgId) {
      try {
        await saveProjectSpecLinks(pid, orgId, remaining);
      } catch {
        /* project links also cascade when temp docs are deleted */
      }
    }
    if (remaining.length > 0 && pid) {
      publishProjectSpecsToHost(pid, remaining, remainingRows);
    } else {
      clearProjectSpecsFromHost();
      setProjectNotice("");
    }
    setSelectedIds([]);
    fetchList();
  };

  // 기간연장 (선택된 항목 일괄)
  const executeExtend = async () => {
    if (selectedIds.length === 0) return;
    setShowExtendModal(false);
    const ids = [...selectedIds];
    const qBase = `org_id=${encodeURIComponent(orgId)}&extra_months=${extendMonths}`;
    for (const id of ids) {
      try {
        await fetch(`${API_BASE}/temp/${id}/extend?${qBase}`, {
          method: "PATCH",
          headers: { ...getDocumentApiHeaders() },
        });
      } catch {
        /* swallow */
      }
    }
    setSelectedIds([]);
    fetchList();
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files) setFiles(Array.from(e.target.files).slice(0, 1));
    if (e.target) e.target.value = "";
  };

  const filteredList = specList.filter(
    (s) =>
      s.file_name.toLowerCase().includes(searchQuery.toLowerCase()) ||
      (s.comment ?? "").toLowerCase().includes(searchQuery.toLowerCase()),
  );

  const fmtDate = (iso: string) => {
    const d = new Date(iso);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  };

  const toggleSelectAll = () => {
    setSelectedIds(
      selectedIds.length === filteredList.length
        ? []
        : filteredList.map((s) => s.id),
    );
  };

  const closeModal = () => {
    if (window.chrome?.webview)
      window.chrome.webview.postMessage("CLOSE_MODAL");
    onClose();
  };

  if (!isOpen) return null;

  return (
    <div
      style={{
        width: "100%",
        height: "100%",
        display: "flex",
        flexDirection: "column",
        background: T.bg,
        color: T.text,
        fontFamily: "'Segoe UI', 'Malgun Gothic', sans-serif",
        fontSize: "13px",
        overflow: "hidden",
        position: "relative",
      }}
    >
      {/* 닫기 버튼 */}
      <div
        onClick={closeModal}
        style={{
          position: "absolute",
          top: "10px",
          right: "12px",
          cursor: "pointer",
          color: T.textMuted,
          zIndex: 1000,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          width: "24px",
          height: "24px",
          borderRadius: "4px",
        }}
        onMouseEnter={(e) =>
          (e.currentTarget.style.background = "rgba(255,255,255,0.1)")
        }
        onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
      >
        <X size={18} />
      </div>

      {/* API 키 미등록 가드 */}
      {!isApiKeyRegistered && (
        <div
          style={{
            position: "absolute",
            inset: 0,
            background: "rgba(0,0,0,0.85)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            zIndex: 4000,
          }}
        >
          <div
            style={{
              width: "320px",
              background: T.panelBg,
              border: `1px solid ${T.border}`,
              borderRadius: "8px",
              padding: "24px",
              textAlign: "center",
            }}
          >
            <AlertCircle
              size={40}
              style={{ color: T.danger, marginBottom: "12px" }}
            />
            <div
              style={{
                fontWeight: 600,
                fontSize: "14px",
                marginBottom: "8px",
                color: T.white,
              }}
            >
              API 키 미등록
            </div>
            <div style={{ color: T.textSub, marginBottom: "16px" }}>
              시방서 관리를 사용하려면 먼저 설정에서 API 키를 등록해주세요.
            </div>
            <button
              onClick={closeModal}
              style={{
                padding: "8px 24px",
                background: T.accent,
                color: "#fff",
                border: "none",
                borderRadius: "4px",
                cursor: "pointer",
                fontWeight: 600,
              }}
            >
              닫기
            </button>
          </div>
        </div>
      )}

      {/* 탭 바 */}
      <div
        style={{
          display: "flex",
          alignItems: "stretch",
          background: T.panelBg,
          borderBottom: `1px solid ${T.border}`,
          height: "40px",
          flexShrink: 0,
        }}
      >
        {(["upload", "manage"] as const).map((tab) => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            style={{
              display: "flex",
              alignItems: "center",
              gap: "6px",
              padding: "0 20px",
              background: activeTab === tab ? T.bg : "transparent",
              color: activeTab === tab ? T.white : T.textSub,
              border: "none",
              borderBottom:
                activeTab === tab
                  ? `2px solid ${T.accent}`
                  : "2px solid transparent",
              cursor: "pointer",
              fontSize: "13px",
              fontWeight: activeTab === tab ? 600 : 400,
            }}
          >
            {tab === "upload" ? (
              <>
                <Upload size={14} /> 시방서 등록
              </>
            ) : (
              <>
                <FolderOpen size={14} /> 시방서 관리
              </>
            )}
          </button>
        ))}
        <div style={{ flex: 1 }} />
      </div>

      {/* 메인 콘텐츠 */}
      <div
        style={{
          flex: 1,
          overflow: "hidden",
          display: "flex",
          flexDirection: "column",
        }}
      >
        {/* === 시방서 등록 탭 === */}
        {activeTab === "upload" && (
          <div
            style={{
              flex: 1,
              display: "flex",
              flexDirection: "column",
              padding: "24px 32px",
              overflowY: "auto",
            }}
          >
            {/* 파일 선택 */}
            <Section title="시방서 파일 선택">
              <div
                style={{ display: "flex", gap: "10px", marginBottom: "10px" }}
              >
                <div
                  style={{
                    flex: 1,
                    height: "38px",
                    background: T.inputBg,
                    border: `1px solid ${T.border}`,
                    borderRadius: "4px",
                    display: "flex",
                    alignItems: "center",
                    padding: "0 12px",
                    color: T.textSub,
                  }}
                >
                  {files.length > 0
                    ? files[0].name
                    : "업로드할 시방서(PDF)를 선택해주세요"}
                </div>
                <button
                  onClick={() => fileInputRef.current?.click()}
                  style={{
                    padding: "0 20px",
                    height: "38px",
                    background: T.cardBg,
                    color: T.white,
                    border: `1px solid ${T.borderLight}`,
                    borderRadius: "4px",
                    cursor: "pointer",
                    fontSize: "13px",
                  }}
                >
                  찾아보기
                </button>
              </div>
              <input
                ref={fileInputRef}
                type="file"
                accept=".pdf"
                style={{ display: "none" }}
                onChange={handleFileChange}
              />
            </Section>

            {/* 시방서 코멘트 */}
            <Section title="시방서 코멘트">
              <textarea
                value={comment}
                onChange={(e) => setComment(e.target.value)}
                placeholder="설명을 입력해주세요 (선택)"
                style={{
                  width: "100%",
                  height: "80px",
                  background: T.inputBg,
                  color: T.white,
                  border: `1px solid ${T.border}`,
                  borderRadius: "4px",
                  padding: "10px",
                  outline: "none",
                  resize: "none",
                  fontSize: "13px",
                }}
              />
            </Section>

            {/* 도메인 + 보관 기간 (커스텀 드롭다운: 아래 펼침·스크롤) */}
            <div
              style={{
                display: "flex",
                gap: "12px",
                alignItems: "flex-start",
                marginBottom: "20px",
              }}
            >
              <div style={{ flex: 1, minWidth: 0 }}>
                <Section title="도메인 선택">
                  <CustomSelect
                    value={domain ?? ""}
                    placeholder="선택해주세요"
                    options={DOMAIN_OPTIONS}
                    onChange={(v) => setDomain(v === "" ? null : v)}
                  />
                </Section>
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <Section title="파일 보관 기간">
                  <CustomSelect
                    value={
                      retentionMonths === null ? "" : String(retentionMonths)
                    }
                    placeholder="선택해주세요"
                    options={Array.from({ length: 12 }, (_, i) => ({
                      value: String(i + 1),
                      label: `${i + 1}개월`,
                    }))}
                    onChange={(v) =>
                      setRetentionMonths(v === "" ? null : Number(v))
                    }
                  />
                </Section>
              </div>
            </div>

            {/* 버튼 */}
            <div
              style={{
                display: "flex",
                justifyContent: "flex-end",
                gap: "10px",
                marginTop: "auto",
                padding: "20px 0",
                borderTop: `1px solid ${T.border}`,
              }}
            >
              <button
                onClick={handleUpload}
                disabled={
                  files.length === 0 ||
                  isUploading ||
                  domain == null ||
                  retentionMonths == null
                }
                style={{
                  padding: "0 22px",
                  height: "38px",
                  background:
                    files.length === 0 ||
                    isUploading ||
                    domain == null ||
                    retentionMonths == null
                      ? T.border
                      : T.accent,
                  color: "#fff",
                  border: "none",
                  borderRadius: "4px",
                  cursor:
                    files.length === 0 ||
                    isUploading ||
                    domain == null ||
                    retentionMonths == null
                      ? "not-allowed"
                      : "pointer",
                  fontWeight: 600,
                }}
              >
                저장
              </button>
              <button
                onClick={closeModal}
                style={{
                  padding: "0 22px",
                  height: "38px",
                  background: "transparent",
                  color: T.text,
                  border: `1px solid ${T.border}`,
                  borderRadius: "4px",
                  cursor: "pointer",
                }}
              >
                닫기
              </button>
            </div>
          </div>
        )}

        {/* === 시방서 관리 탭 === */}
        {activeTab === "manage" && (
          <div
            style={{
              flex: 1,
              display: "flex",
              flexDirection: "column",
              overflow: "hidden",
            }}
          >
            {/* 검색 + 삭제 */}
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: "8px",
                padding: "8px 12px",
                background: T.panelBg,
                borderBottom: `1px solid ${T.border}`,
                flexShrink: 0,
              }}
            >
              <div
                style={{
                  flex: 1,
                  maxWidth: "250px",
                  display: "flex",
                  alignItems: "center",
                  background: T.inputBg,
                  border: `1px solid ${T.border}`,
                  borderRadius: "3px",
                  padding: "0 8px",
                }}
              >
                <Search size={14} style={{ color: T.textMuted }} />
                <input
                  type="text"
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  placeholder="검색..."
                  style={{
                    flex: 1,
                    padding: "6px",
                    background: "transparent",
                    border: "none",
                    color: T.text,
                    fontSize: "12px",
                    outline: "none",
                  }}
                />
              </div>
              <div style={{ flex: 1 }} />
              <button
                type="button"
                onClick={saveSelectedSpecsToProject}
                disabled={isApplyingProject}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "4px",
                  padding: "5px 12px",
                  background:
                    isApplyingProject
                      ? "transparent"
                      : "rgba(78,201,176,0.12)",
                  color:
                    isApplyingProject
                      ? T.textMuted
                      : T.success,
                  border: `1px solid ${T.border}`,
                  borderRadius: "3px",
                  fontSize: "12px",
                  cursor:
                    isApplyingProject
                      ? "not-allowed"
                      : "pointer",
                }}
              >
                <CheckCircle size={13} /> {selectedIds.length === 0 ? "적용 해제" : "프로젝트 적용"}
              </button>
              <button
                type="button"
                onClick={() => {
                  if (selectedIds.length === 0) return;
                  setExtendMonths(1);
                  setShowExtendModal(true);
                }}
                disabled={selectedIds.length === 0}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "4px",
                  padding: "5px 12px",
                  background:
                    selectedIds.length === 0
                      ? "transparent"
                      : "rgba(0,120,212,0.12)",
                  color: selectedIds.length === 0 ? T.textMuted : T.accent,
                  border: `1px solid ${T.border}`,
                  borderRadius: "3px",
                  fontSize: "12px",
                  cursor: selectedIds.length === 0 ? "not-allowed" : "pointer",
                }}
              >
                <Clock size={13} /> 연장
              </button>
              <button
                type="button"
                onClick={() =>
                  selectedIds.length > 0 && setShowDeleteConfirm(true)
                }
                disabled={selectedIds.length === 0}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "4px",
                  padding: "5px 12px",
                  background:
                    selectedIds.length === 0
                      ? "transparent"
                      : "rgba(241,76,76,0.1)",
                  color: selectedIds.length === 0 ? T.textMuted : T.danger,
                  border: `1px solid ${T.border}`,
                  borderRadius: "3px",
                  fontSize: "12px",
                  cursor: selectedIds.length === 0 ? "not-allowed" : "pointer",
                }}
              >
                <Trash2 size={13} /> 삭제
              </button>
            </div>

            {projectNotice && (
              <div
                style={{
                  margin: "8px 12px",
                  padding: "8px 10px",
                  border: `1px solid ${T.border}`,
                  borderRadius: "4px",
                  background: T.cardBg,
                  color: T.textSub,
                  fontSize: "12px",
                }}
              >
                {projectNotice}
              </div>
            )}

            {/* 테이블 헤더 */}
            <div
              style={{
                display: "flex",
                alignItems: "center",
                padding: "0 12px",
                height: "32px",
                background: T.cardBg,
                borderBottom: `1px solid ${T.border}`,
                fontSize: "11px",
                color: T.textSub,
                fontWeight: 600,
              }}
            >
              <div style={{ width: "30px", textAlign: "center" }}>
                <input
                  type="checkbox"
                  checked={
                    selectedIds.length === filteredList.length &&
                    filteredList.length > 0
                  }
                  onChange={toggleSelectAll}
                />
              </div>
              <div style={{ flex: 2, paddingLeft: "8px" }}>파일명</div>
              <div style={{ flex: 2 }}>코멘트</div>
              <div style={{ flex: 1, textAlign: "center" }}>상태</div>
              <div style={{ flex: 1, textAlign: "center" }}>등록일</div>
              <div style={{ flex: 1, textAlign: "center" }}>파기일</div>
            </div>

            {/* 목록 */}
            <div style={{ flex: 1, overflowY: "auto" }}>
              {isLoading ? (
                <div
                  style={{
                    textAlign: "center",
                    padding: "40px",
                    color: T.textSub,
                  }}
                >
                  불러오는 중...
                </div>
              ) : filteredList.length === 0 ? (
                <div
                  style={{
                    textAlign: "center",
                    padding: "40px",
                    color: T.textMuted,
                  }}
                >
                  등록된 시방서가 없습니다.
                </div>
              ) : (
                filteredList.map((spec) => (
                  <div
                    key={spec.id}
                    onClick={() =>
                      setSelectedIds((p) =>
                        p.includes(spec.id)
                          ? p.filter((i) => i !== spec.id)
                          : [...p, spec.id],
                      )
                    }
                    style={{
                      display: "flex",
                      alignItems: "center",
                      padding: "8px 12px",
                      borderBottom: `1px solid ${T.border}`,
                      background: selectedIds.includes(spec.id)
                        ? T.selected
                        : "transparent",
                      cursor: "pointer",
                      fontSize: "12px",
                    }}
                  >
                    <div style={{ width: "30px", textAlign: "center" }}>
                      <input
                        type="checkbox"
                        checked={selectedIds.includes(spec.id)}
                        readOnly
                      />
                    </div>
                    <div
                      style={{
                        flex: 2,
                        fontWeight: 600,
                        paddingLeft: "8px",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {spec.file_name}
                    </div>
                    <div
                      style={{
                        flex: 2,
                        color: T.textSub,
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {spec.comment || "-"}
                    </div>
                    <div style={{ flex: 1, textAlign: "center" }}>
                      <span
                        style={{
                          padding: "2px 8px",
                          borderRadius: "3px",
                          fontSize: "11px",
                          background:
                            spec.status === "completed"
                              ? "rgba(78,201,176,0.15)"
                              : "rgba(255,200,0,0.15)",
                          color:
                            spec.status === "completed" ? T.success : "#f0c040",
                        }}
                      >
                        {spec.status === "completed"
                          ? "완료"
                          : spec.status === "error"
                            ? "오류"
                            : "처리중"}
                      </span>
                    </div>
                    <div
                      style={{ flex: 1, textAlign: "center", color: T.textSub }}
                    >
                      {fmtDate(spec.reg_date)}
                    </div>
                    <div
                      style={{ flex: 1, textAlign: "center", color: T.danger }}
                    >
                      {fmtDate(spec.delete_date)}
                    </div>
                  </div>
                ))
              )}
            </div>
          </div>
        )}
      </div>

      {/* 업로드 진행 오버레이 */}
      {isUploading && (
        <Overlay>
          <div
            style={{
              width: "320px",
              background: T.panelBg,
              border: `1px solid ${T.accent}`,
              borderRadius: "8px",
              padding: "32px",
              textAlign: "center",
            }}
          >
            <Loader2
              size={40}
              style={{
                color: T.accent,
                marginBottom: "16px",
                animation: "spin 1s linear infinite",
              }}
            />
            <div
              style={{
                fontWeight: 600,
                fontSize: "14px",
                marginBottom: "8px",
                color: T.white,
              }}
            >
              AI가 시방서를 분석 중입니다...
            </div>
            <div style={{ color: T.textSub, fontSize: "12px" }}>
              파싱 및 임베딩 생성까지 1~3분 소요됩니다.
            </div>
          </div>
          <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
        </Overlay>
      )}

      {/* 업로드 결과 모달 */}
      {uploadResult && (
        <Overlay>
          <div
            style={{
              width: "320px",
              background: T.panelBg,
              border: `1px solid ${uploadResult.type === "success" ? T.success : T.danger}`,
              borderRadius: "8px",
              overflow: "hidden",
            }}
          >
            <div
              style={{
                padding: "12px 16px",
                background:
                  uploadResult.type === "success"
                    ? "rgba(78,201,176,0.1)"
                    : "rgba(241,76,76,0.1)",
                borderBottom: `1px solid ${T.border}`,
                fontWeight: 600,
                display: "flex",
                alignItems: "center",
                gap: "8px",
                color: uploadResult.type === "success" ? T.success : T.danger,
              }}
            >
              {uploadResult.type === "success" ? (
                <CheckCircle size={16} />
              ) : (
                <AlertCircle size={16} />
              )}
              {uploadResult.title}
            </div>
            <div
              style={{
                padding: "20px",
                color: T.text,
                whiteSpace: "pre-line",
                fontSize: "13px",
              }}
            >
              {uploadResult.message}
            </div>
            <div style={{ padding: "12px", display: "flex", gap: "8px" }}>
              <button
                onClick={() => {
                  setUploadResult(null);
                  if (uploadResult.type === "success") setActiveTab("manage");
                }}
                style={{
                  flex: 1,
                  padding: "8px",
                  background:
                    uploadResult.type === "success" ? T.success : T.accent,
                  color: "#fff",
                  border: "none",
                  borderRadius: "4px",
                  cursor: "pointer",
                  fontWeight: 600,
                }}
              >
                {uploadResult.type === "success" ? "관리 탭으로 이동" : "확인"}
              </button>
              {uploadResult.type === "error" && (
                <button
                  onClick={() => {
                    setUploadResult(null);
                    handleUpload();
                  }}
                  style={{
                    flex: 1,
                    padding: "8px",
                    background: T.accent,
                    color: "#fff",
                    border: "none",
                    borderRadius: "4px",
                    cursor: "pointer",
                    fontWeight: 600,
                  }}
                >
                  재시도
                </button>
              )}
            </div>
          </div>
        </Overlay>
      )}

      {/* 삭제 확인 모달 */}
      {showDeleteConfirm && (
        <Overlay>
          <div
            style={{
              width: "300px",
              background: T.panelBg,
              border: `1px solid ${T.danger}`,
              borderRadius: "8px",
              overflow: "hidden",
            }}
          >
            <div
              style={{
                padding: "12px 16px",
                background: "rgba(241,76,76,0.1)",
                borderBottom: `1px solid ${T.border}`,
                fontWeight: 600,
                color: T.danger,
              }}
            >
              삭제 확인
            </div>
            <div
              style={{ padding: "20px", textAlign: "center", color: T.text }}
            >
              선택한 {selectedIds.length}개 시방서를 삭제하시겠습니까?
            </div>
            <div style={{ padding: "12px", display: "flex", gap: "8px" }}>
              <button
                onClick={executeDelete}
                style={{
                  flex: 1,
                  padding: "8px",
                  background: T.danger,
                  color: "#fff",
                  border: "none",
                  borderRadius: "4px",
                  cursor: "pointer",
                }}
              >
                예
              </button>
              <button
                onClick={() => setShowDeleteConfirm(false)}
                style={{
                  flex: 1,
                  padding: "8px",
                  background: "transparent",
                  color: T.text,
                  border: `1px solid ${T.border}`,
                  borderRadius: "4px",
                  cursor: "pointer",
                }}
              >
                아니오
              </button>
            </div>
          </div>
        </Overlay>
      )}

      {/* 기간연장 모달 (관리 탭에서 항목 선택 후 툴바 «연장») */}
      {showExtendModal && (
        <Overlay>
          <div
            style={{
              width: "320px",
              background: T.panelBg,
              border: `1px solid ${T.accent}`,
              borderRadius: "8px",
              overflow: "hidden",
            }}
          >
            <div
              style={{
                padding: "12px 16px",
                background: "rgba(0,120,212,0.1)",
                borderBottom: `1px solid ${T.border}`,
                fontWeight: 600,
                display: "flex",
                alignItems: "center",
                gap: "8px",
                color: T.white,
              }}
            >
              <Clock size={16} style={{ color: T.accent }} />
              보관기간 연장
            </div>
            <div
              style={{
                padding: "20px",
                color: T.text,
                fontSize: "13px",
                lineHeight: 1.5,
              }}
            >
              선택한{" "}
              <strong style={{ color: T.white }}>{selectedIds.length}개</strong>{" "}
              시방서의 파기 예정일을 연장합니다.
            </div>
            <div style={{ padding: "0 20px 20px" }}>
              <div
                style={{
                  marginBottom: "8px",
                  fontSize: "12px",
                  color: T.textSub,
                }}
              >
                연장 기간
              </div>
              <select
                value={extendMonths}
                onChange={(e) => setExtendMonths(Number(e.target.value))}
                style={{
                  width: "100%",
                  height: "36px",
                  background: T.inputBg,
                  color: T.white,
                  border: `1px solid ${T.border}`,
                  borderRadius: "4px",
                  padding: "0 12px",
                  outline: "none",
                  fontSize: "13px",
                }}
              >
                {EXTEND_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </div>
            <div style={{ padding: "12px", display: "flex", gap: "8px" }}>
              <button
                type="button"
                onClick={executeExtend}
                style={{
                  flex: 1,
                  padding: "8px",
                  background: T.accent,
                  color: "#fff",
                  border: "none",
                  borderRadius: "4px",
                  cursor: "pointer",
                  fontWeight: 600,
                }}
              >
                연장 적용
              </button>
              <button
                type="button"
                onClick={() => setShowExtendModal(false)}
                style={{
                  flex: 1,
                  padding: "8px",
                  background: "transparent",
                  color: T.text,
                  border: `1px solid ${T.border}`,
                  borderRadius: "4px",
                  cursor: "pointer",
                }}
              >
                취소
              </button>
            </div>
          </div>
        </Overlay>
      )}
    </div>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div style={{ marginBottom: "20px" }}>
      <div
        style={{
          marginBottom: "8px",
          color: "#e8e8e8",
          fontSize: "14px",
          fontWeight: 600,
        }}
      >
        {title}
      </div>
      {children}
    </div>
  );
}

function Overlay({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        position: "absolute",
        inset: 0,
        background: "rgba(0,0,0,0.7)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 3000,
      }}
    >
      {children}
    </div>
  );
}
