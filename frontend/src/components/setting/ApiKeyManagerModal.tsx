/*
 * File    : frontend/src/components/setting/ApiKeyManagerModal.tsx
 * Author  : 김지우
 * Create  : 2026-04-16
 *
 * Description :
 *   API Key 등록 모달.
 *   - C# PluginEntry가 WebView2로 전송하는 MACHINE_INFO 메시지를 수신하여
 *     machine_id / hostname / os_user 를 자동으로 캡처합니다.
 *   - 등록 시 백엔드 /api/v1/licenses/verify 에 machine_id 포함 POST →
 *     devices 테이블 자동 upsert (C# AutoCAD 플러그인 없이는 machine_id = "").
 *   - 성공 응답의 org_id / device_id 를 localStorage 에 저장합니다.
 *   - '라이선스 목록' 탭: GET /api/v1/licenses/devices?org_id=... 로 실 DB 조회.
 *
 * Modification History :
 *   - 2026-04-20 : MACHINE_INFO 수신 + 실 API 연동 + 디바이스 목록 실 DB 조회로 전환
 */

import React, { useState, useEffect, useCallback } from "react";
import {
  Key,
  Eye,
  EyeOff,
  Info,
  Check,
  AlertCircle,
  X,
  Trash2,
  Monitor,
  RefreshCw,
} from "lucide-react";

// VITE_API_BASE_URL 환경변수로 EC2/원격 서버 URL 주입 가능. 기본값 localhost:8000
const API_BASE = ((import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") || "http://localhost:8000") + "/api/v1";

// ── AutoCAD 다크 테마 팔레트 ──────────────────────────────────────────────
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
  danger: "#f14c4c",
  white: "#e8e8e8",
  success: "#4EC9B0",
} as const;

// ── 타입 ──────────────────────────────────────────────────────────────────
interface MachineInfo {
  machine_id: string;
  hostname: string;
  os_user: string;
}

interface DeviceEntry {
  id: string;
  machine_id: string;
  display_name: string;
  hostname: string;
  os_user: string;
  is_active: boolean;
  first_seen: string;
  last_seen: string;
}

interface Notice {
  isOpen: boolean;
  title: string;
  message: string;
  type: "success" | "error" | "info";
}

interface Props {
  isOpen: boolean;
  onClose: () => void;
}

// ── 헬퍼 ──────────────────────────────────────────────────────────────────
const fmtDate = (iso: string) => {
  if (!iso) return "-";
  try {
    return new Date(iso).toLocaleDateString("ko-KR");
  } catch {
    return iso.slice(0, 10);
  }
};

const fmtDateTime = (iso: string) => {
  if (!iso) return "-";
  try {
    return new Date(iso).toLocaleString("ko-KR", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso.slice(0, 16);
  }
};

// ── 컴포넌트 ──────────────────────────────────────────────────────────────
export default function ApiKeyManagerModal({ isOpen, onClose }: Props) {
  // ── 초기 상태 ─────────────────────────────────────────────────────────
  const [isKeyRegistered, setIsKeyRegistered] = useState(
    () => localStorage.getItem("skn23_api_key_registered") === "true",
  );
  const [activeTab, setActiveTab] = useState<"register" | "manage">(
    isKeyRegistered ? "manage" : "register",
  );

  // ── 입력 / 검증 상태 ──────────────────────────────────────────────────
  const [apiKey, setApiKey] = useState(
    () => localStorage.getItem("skn23_api_key") || "",
  );
  const [showKey, setShowKey] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [authError, setAuthError] = useState("");

  // ── 노트북 식별 정보 (C# MACHINE_INFO 수신) ─────────────────────────
  const [machineInfo, setMachineInfo] = useState<MachineInfo>({
    machine_id: "",
    hostname: "",
    os_user: "",
  });

  // ── 플랜 정보 ───────────────────────────────────────────────────────
  const [planInfo, setPlanInfo] = useState(() => ({
    plan: localStorage.getItem("skn23_plan") || "basic",
    maxSeats: parseInt(localStorage.getItem("skn23_max_seats") || "5", 10),
    companyName: localStorage.getItem("skn23_company_name") || "",
  }));

  // ── 디바이스 목록 (관리 탭) ───────────────────────────────────────────
  const [deviceList, setDeviceList] = useState<DeviceEntry[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [myDeviceId, setMyDeviceId] = useState(
    () => localStorage.getItem("skn23_device_id") || "",
  );

  // ── 모달 상태 ─────────────────────────────────────────────────────────
  const [showConfirmModal, setShowConfirmModal] = useState(false);
  const [showDeleteModal, setShowDeleteModal] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice>({
    isOpen: false,
    title: "",
    message: "",
    type: "info",
  });

  // ── machine_id 초기화: C#에서 WebView2로 전송한 MACHINE_INFO (localStorage) 우선 사용 ──
  useEffect(() => {
    // C# 플러그인이 ChatApp에서 localStorage에 저장한 값을 우선 사용
    const savedMid  = localStorage.getItem("skn23_machine_id") || "";
    const savedHost = localStorage.getItem("skn23_hostname") || "";
    const savedUser = localStorage.getItem("skn23_os_user") || "";

    if (savedMid && !savedMid.startsWith("browser-")) {
      setMachineInfo({
        machine_id: savedMid,
        hostname: savedHost,
        os_user: savedUser,
      });
      return;
    }

    // C# 데이터가 없으면 서버 API 폴백 (Docker 컨테이너 ID가 올 수 있음)
    fetch(`${API_BASE}/licenses/machine-info`)
      .then((res) => res.json())
      .then((data) => {
        const info: MachineInfo = {
          machine_id: data.machine_id || "",
          hostname: data.hostname || "",
          os_user: data.os_user || "",
        };
        setMachineInfo(info);
        localStorage.setItem("skn23_machine_id", info.machine_id);
        localStorage.setItem("skn23_hostname", info.hostname);
        localStorage.setItem("skn23_os_user", info.os_user);
      })
      .catch(() => {});
  }, []);

  // ── 디바이스 목록 조회 ────────────────────────────────────────────────
  const fetchDevices = useCallback(async () => {
    const orgId = localStorage.getItem("skn23_org_id");
    // UUID 형식이 아닌 잘못된 잔류값(demo-org-id 등)은 무시
    if (!orgId || orgId.length < 32) {
      setDeviceList([]);
      return;
    }

    setIsLoading(true);
    try {
      const res = await fetch(
        `${API_BASE}/licenses/devices?org_id=${encodeURIComponent(orgId)}`,
      );
      if (!res.ok) throw new Error(`${res.status}`);
      const data: DeviceEntry[] = await res.json();
      const myId = localStorage.getItem("skn23_device_id") || "";
      setDeviceList([...data].sort((a, b) => (a.id === myId ? -1 : b.id === myId ? 1 : 0)));
    } catch (e) {
      console.error("디바이스 목록 조회 실패:", e);
      setDeviceList([]);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (activeTab === "manage") fetchDevices();
  }, [activeTab, fetchDevices]);

  // ── 핸들러 ────────────────────────────────────────────────────────────
  const showAlert = (
    title: string,
    message: string,
    type: Notice["type"] = "info",
  ) => setNotice({ isOpen: true, title, message, type });

  const handleRegister = async () => {
    setAuthError("");
    if (!apiKey.trim()) {
      setAuthError("API Key를 입력해주세요.");
      return;
    }

    setIsSaving(true);
    try {
      const res = await fetch(`${API_BASE}/licenses/verify`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          api_key: apiKey.trim(),
          machine_id:
            machineInfo.machine_id ||
            localStorage.getItem("skn23_machine_id") ||
            undefined,
          // 빈 문자열이면 키 생략(서버가 이전 NULL 덮기) — 값이 있을 때만 전송
          hostname:
            machineInfo.hostname ||
            localStorage.getItem("skn23_hostname") ||
            undefined,
          os_user:
            machineInfo.os_user || localStorage.getItem("skn23_os_user") || undefined,
        }),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        setAuthError(
          err.detail || "유효하지 않은 키입니다. 다시 확인해주세요.",
        );
        setShowConfirmModal(false);
        return;
      }

      const data = await res.json();

      // 서버가 실제 machine_id/hostname을 돌려주면 갱신
      const realMid = data.machine_id || machineInfo.machine_id;
      const realHost = data.hostname || machineInfo.hostname;

      // localStorage 저장 (이후 API 호출에서 활용)
      localStorage.setItem("skn23_api_key", apiKey.trim());
      localStorage.setItem("skn23_api_key_registered", "true");
      localStorage.setItem("skn23_org_id", data.org_id ?? "");
      localStorage.setItem("skn23_device_id", data.device_id ?? "");
      localStorage.setItem("skn23_plan", data.plan ?? "basic");
      localStorage.setItem("skn23_max_seats", String(data.max_seats ?? 5));
      localStorage.setItem("skn23_company_name", data.company_name ?? "");
      localStorage.setItem("skn23_machine_id", realMid);
      localStorage.setItem("skn23_hostname", realHost);

      setMachineInfo((prev) => ({
        ...prev,
        machine_id: realMid,
        hostname: realHost,
      }));
      setMyDeviceId(data.device_id ?? "");
      setPlanInfo({
        plan: data.plan ?? "basic",
        maxSeats: data.max_seats ?? 5,
        companyName: data.company_name ?? "",
      });
      setIsKeyRegistered(true);
      // 등록 완료 즉시 C#에 SESSION_INFO 전달 (S3 경로 정상화)
      if (data.org_id && data.device_id && window.chrome?.webview) {
        window.chrome.webview.postMessage(
          JSON.stringify({
            action: "SESSION_INFO",
            payload: { org_id: data.org_id, device_id: data.device_id },
          }),
        );
      }
      setShowConfirmModal(false);
      setShowKey(false);
      setActiveTab("manage");
      showAlert(
        "등록 성공",
        data.device_id
          ? `API 키가 등록되고 이 기기(${realHost || "현재 기기"})가 자동으로 연동되었습니다.`
          : "API 키가 성공적으로 등록되었습니다.",
        "success",
      );
    } catch {
      setAuthError("서버와의 통신 중 오류가 발생했습니다.");
      setShowConfirmModal(false);
    } finally {
      setIsSaving(false);
    }
  };

  const handleDeleteDevice = async () => {
    if (!showDeleteModal) return;
    try {
      const res = await fetch(
        `${API_BASE}/licenses/devices/${showDeleteModal}`,
        {
          method: "DELETE",
        },
      );
      if (!res.ok) throw new Error();
      if (showDeleteModal === myDeviceId) {
        localStorage.removeItem("skn23_device_id");
        setMyDeviceId("");
      }
      setShowDeleteModal(null);
      showAlert("삭제 완료", "기기가 삭제되었습니다.", "success");
      fetchDevices();
    } catch {
      showAlert("오류", "기기 삭제 중 문제가 발생했습니다.", "error");
      setShowDeleteModal(null);
    }
  };

  const handleClose = () => {
    window.chrome?.webview?.postMessage("CLOSE_MODAL");
    onClose();
  };

  if (!isOpen) return null;

  const activeDevices = deviceList;

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
        onClick={handleClose}
        style={{
          position: "absolute",
          top: "16px",
          right: "20px",
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

      {/* ── 헤더 ── */}
      <div style={{ padding: "32px 32px 24px" }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "10px",
            marginBottom: "8px",
          }}
        >
          <Key size={24} style={{ color: T.accent }} />
          <h1
            style={{
              color: T.white,
              fontSize: "22px",
              fontWeight: 700,
              margin: 0,
            }}
          >
            API 관리
          </h1>
        </div>
        <p style={{ color: T.textSub, fontSize: "13px", margin: 0 }}>
          API 키와 기기 라이선스를 관리합니다.
        </p>
        {/* 현재 기기 정보 표시 (C#에서 받은 경우) */}
        {machineInfo.machine_id && (
          <div
            style={{
              marginTop: "10px",
              display: "flex",
              alignItems: "center",
              gap: "6px",
              color: T.textSub,
              fontSize: "11px",
            }}
          >
            <Monitor size={12} style={{ color: T.accent }} />
            현재 기기:{" "}
            <span style={{ color: T.white }}>{machineInfo.hostname}</span>
            &nbsp;({machineInfo.os_user})
          </div>
        )}
      </div>

      {/* ── 탭 바 ── */}
      <div style={{ padding: "0 32px 24px" }}>
        <div
          style={{
            display: "flex",
            background: T.panelBg,
            borderRadius: "8px",
            padding: "4px",
            border: `1px solid ${T.border}`,
          }}
        >
          {(["register", "manage"] as const).map((tab) => (
            <button
              key={tab}
              onClick={() => {
                if (tab === "manage" && !isKeyRegistered) return;
                setActiveTab(tab);
              }}
              disabled={tab === "manage" && !isKeyRegistered}
              style={{
                flex: 1,
                height: "36px",
                border: "none",
                borderRadius: "6px",
                cursor:
                  tab === "manage" && !isKeyRegistered
                    ? "not-allowed"
                    : "pointer",
                fontSize: "13px",
                fontWeight: 600,
                transition: "all 0.2s ease",
                background: activeTab === tab ? T.accent : "transparent",
                color:
                  activeTab === tab
                    ? "#fff"
                    : tab === "manage" && !isKeyRegistered
                      ? T.textMuted
                      : T.textSub,
                opacity: tab === "manage" && !isKeyRegistered ? 0.5 : 1,
              }}
            >
              {tab === "register" ? "등록" : "기기 목록"}
            </button>
          ))}
        </div>
      </div>

      {/* ── 콘텐츠 ── */}
      <div
        style={{
          flex: 1,
          overflowY: "auto",
          padding: "0 32px 32px",
          display: "flex",
          flexDirection: "column",
        }}
      >
        {/* 1. 등록 탭 */}
        {activeTab === "register" && (
          <div style={{ flex: 1, display: "flex", flexDirection: "column" }}>
            <div style={{ marginBottom: "24px" }}>
              <label
                style={{
                  display: "block",
                  color: T.text,
                  fontSize: "13px",
                  fontWeight: 600,
                  marginBottom: "12px",
                }}
              >
                API 키
              </label>
              <div style={{ position: "relative" }}>
                <input
                  type={showKey ? "text" : "password"}
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder="발급받은 API 키를 입력하세요 (데모: 1234)"
                  style={{
                    width: "100%",
                    padding: "16px 50px 16px 16px",
                    background: T.inputBg,
                    color: T.white,
                    border: `1px solid ${T.borderLight}`,
                    borderRadius: "8px",
                    outline: "none",
                    fontSize: "13px",
                    fontFamily: "monospace",
                    boxSizing: "border-box",
                  }}
                  onFocus={(e) =>
                    (e.currentTarget.style.borderColor = T.accent)
                  }
                  onBlur={(e) =>
                    (e.currentTarget.style.borderColor = T.borderLight)
                  }
                  onKeyDown={(e) => e.key === "Enter" && requestRegister()}
                />
                <button
                  onClick={() => setShowKey(!showKey)}
                  style={{
                    position: "absolute",
                    right: "12px",
                    top: "50%",
                    transform: "translateY(-50%)",
                    background: "transparent",
                    border: "none",
                    color: T.textMuted,
                    cursor: "pointer",
                    display: "flex",
                  }}
                >
                  {showKey ? <EyeOff size={18} /> : <Eye size={18} />}
                </button>
              </div>
              <div
                style={{
                  textAlign: "right",
                  color: T.textMuted,
                  fontSize: "11px",
                  marginTop: "6px",
                }}
              >
                {apiKey.length}자
              </div>
            </div>

            {authError && (
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "6px",
                  color: T.danger,
                  fontSize: "12px",
                  marginBottom: "16px",
                }}
              >
                <AlertCircle size={14} /> {authError}
              </div>
            )}

            <div style={{ textAlign: "right", marginBottom: "24px" }}>
              <button
                onClick={requestRegister}
                disabled={isSaving || !apiKey.trim()}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "8px",
                  padding: "10px 24px",
                  background: !apiKey.trim() || isSaving ? T.inputBg : T.accent,
                  color: "#fff",
                  border: "none",
                  borderRadius: "6px",
                  fontWeight: 600,
                  cursor: "pointer",
                }}
              >
                <Key size={16} /> {isSaving ? "등록 중..." : "등록하기"}
              </button>
            </div>

            {/* 자동 연동 안내 */}
            {machineInfo.machine_id && (
              <div
                style={{
                  background: "rgba(0,120,212,0.08)",
                  border: `1px solid ${T.accent}33`,
                  borderRadius: "8px",
                  padding: "14px 18px",
                  marginBottom: "16px",
                  fontSize: "12px",
                  color: T.textSub,
                }}
              >
                <div
                  style={{
                    fontWeight: 600,
                    color: T.accent,
                    marginBottom: "6px",
                    display: "flex",
                    alignItems: "center",
                    gap: "6px",
                  }}
                >
                  <Check size={13} /> 자동 기기 연동 활성
                </div>
                등록 시 현재 기기({machineInfo.hostname})가 devices 테이블에
                자동으로 기록됩니다.
              </div>
            )}

            <div
              style={{
                background: "rgba(0,0,0,0.15)",
                borderRadius: "10px",
                padding: "20px",
                border: `1px solid ${T.border}`,
              }}
            >
              <div
                style={{
                  fontWeight: 600,
                  color: T.white,
                  marginBottom: "12px",
                  display: "flex",
                  alignItems: "center",
                  gap: "6px",
                }}
              >
                <Info size={14} /> 주요 안내사항
              </div>
              <ul
                style={{
                  margin: 0,
                  paddingLeft: "18px",
                  color: T.textSub,
                  fontSize: "12px",
                  listStyleType: "disc",
                  display: "flex",
                  flexDirection: "column",
                  gap: "8px",
                }}
              >
                <li>API 키는 대소문자를 구분합니다.</li>
                <li>각 기기는 라이선스에서 하나의 시트를 차지합니다.</li>
                <li>
                  등록 후 기기 정보(호스트명, 접속 시각)가 자동으로 기록됩니다.
                </li>
                <li>API 키를 안전하게 보관하고 공유하지 마세요.</li>
              </ul>
            </div>
          </div>
        )}

        {/* 2. 기기 목록 탭 */}
        {activeTab === "manage" && (
          <div style={{ flex: 1, display: "flex", flexDirection: "column" }}>
            {/* 사용 현황 */}
            <div
              style={{
                background: T.panelBg,
                borderRadius: "10px",
                padding: "20px",
                border: `1px solid ${T.border}`,
                marginBottom: "24px",
              }}
            >
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  marginBottom: "12px",
                }}
              >
                <span style={{ fontSize: "14px", color: T.textSub }}>
                  활성 기기:{" "}
                  <span
                    style={{
                      color: T.accent,
                      fontWeight: 700,
                      fontSize: "18px",
                    }}
                  >
                    {activeDevices.length}
                  </span>
                  &nbsp;/ 최대 {planInfo.maxSeats}대
                  <span
                    style={{
                      marginLeft: "8px",
                      fontSize: "11px",
                      color: T.accent,
                      background: "rgba(0,120,212,0.15)",
                      padding: "2px 8px",
                      borderRadius: "4px",
                      fontWeight: 600,
                    }}
                  >
                    {planInfo.plan.toUpperCase()}
                  </span>
                </span>
                <button
                  onClick={fetchDevices}
                  disabled={isLoading}
                  title="새로고침"
                  style={{
                    background: "transparent",
                    border: "none",
                    color: T.textSub,
                    cursor: "pointer",
                    display: "flex",
                    alignItems: "center",
                    gap: "4px",
                    fontSize: "12px",
                  }}
                >
                  <RefreshCw
                    size={14}
                    style={{
                      animation: isLoading ? "spin 1s linear infinite" : "none",
                    }}
                  />
                  새로고침
                </button>
              </div>
              <div
                style={{
                  width: "100%",
                  height: "6px",
                  background: T.inputBg,
                  borderRadius: "4px",
                  overflow: "hidden",
                }}
              >
                <div
                  style={{
                    width: `${Math.min((activeDevices.length / planInfo.maxSeats) * 100, 100)}%`,
                    height: "100%",
                    background:
                      activeDevices.length >= planInfo.maxSeats
                        ? T.danger
                        : T.accent,
                    borderRadius: "4px",
                    transition: "width 0.3s",
                  }}
                />
              </div>
            </div>

            {/* 목록 테이블 */}
            <div style={{ flex: 1 }}>
              <div
                style={{
                  display: "flex",
                  padding: "10px 12px",
                  borderBottom: `1px solid ${T.border}`,
                  color: T.textMuted,
                  fontSize: "11px",
                  fontWeight: 600,
                }}
              >
                <div style={{ flex: 2.5 }}>기기 / 사용자</div>
                <div style={{ flex: 2 }}>마지막 접속</div>
                <div style={{ width: "40px", textAlign: "center" }}>관리</div>
              </div>

              {isLoading ? (
                <div
                  style={{
                    padding: "40px",
                    textAlign: "center",
                    color: T.textMuted,
                  }}
                >
                  데이터를 불러오는 중...
                </div>
              ) : deviceList.length === 0 ? (
                <div
                  style={{
                    padding: "40px",
                    textAlign: "center",
                    color: T.textMuted,
                  }}
                >
                  등록된 기기가 없습니다.
                </div>
              ) : (
                deviceList.map((dev) => {
                  const isMe = dev.id === myDeviceId;
                  return (
                    <div
                      key={dev.id}
                      style={{
                        display: "flex",
                        alignItems: "center",
                        padding: "14px 12px",
                        borderBottom: `1px solid ${T.border}`,
                        fontSize: "13px",
                        background: isMe
                          ? "rgba(0,120,212,0.05)"
                          : "transparent",
                      }}
                    >
                      <div style={{ flex: 2.5 }}>
                        <div
                          style={{
                            fontWeight: 600,
                            color: T.white,
                            display: "flex",
                            alignItems: "center",
                            gap: "6px",
                          }}
                        >
                          <Monitor
                            size={13}
                            style={{ color: isMe ? T.accent : T.textSub }}
                          />
                          {dev.display_name}
                          {isMe && (
                            <span
                              style={{
                                fontSize: "10px",
                                color: T.accent,
                                background: "rgba(0,120,212,0.15)",
                                padding: "2px 6px",
                                borderRadius: "10px",
                              }}
                            >
                              현재 기기
                            </span>
                          )}
                        </div>
                        {dev.machine_id && (
                          <div
                            style={{
                              color: T.textMuted,
                              fontSize: "10px",
                              marginTop: "2px",
                              fontFamily: "monospace",
                              opacity: 0.7,
                            }}
                          >
                            GUID: {dev.machine_id}
                          </div>
                        )}
                      </div>
                      <div
                        style={{ flex: 2, color: T.textSub, fontSize: "12px" }}
                      >
                        {fmtDateTime(dev.last_seen)}
                      </div>
                      <div
                        style={{
                          width: "40px",
                          display: "flex",
                          justifyContent: "center",
                        }}
                      >
                        {isMe ? (
                          <button
                            onClick={() => setShowDeleteModal(dev.id)}
                            style={{
                              background: "transparent",
                              border: "none",
                              color: T.danger,
                              cursor: "pointer",
                              display: "flex",
                            }}
                            title="이 기기 삭제"
                          >
                            <Trash2 size={16} />
                          </button>
                        ) : (
                          <div style={{ color: T.textMuted, opacity: 0.3 }}>
                            <Trash2 size={16} />
                          </div>
                        )}
                      </div>
                    </div>
                  );
                })
              )}
            </div>

            {/* 등록 날짜 표 하단 */}
            <div
              style={{
                marginTop: "16px",
                fontSize: "11px",
                color: T.textMuted,
              }}
            >
              * 라이선스 등록일:{" "}
              {deviceList.length > 0
                ? fmtDate(deviceList[deviceList.length - 1].first_seen)
                : "-"}
            </div>
          </div>
        )}
      </div>

      {/* ── 등록 확인 모달 ── */}
      {showConfirmModal && (
        <div
          style={{
            position: "absolute",
            inset: 0,
            background: "rgba(0,0,0,0.7)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            zIndex: 2000,
          }}
        >
          <div
            style={{
              width: "340px",
              background: T.panelBg,
              border: `1px solid ${T.border}`,
              borderRadius: "10px",
              overflow: "hidden",
              boxShadow: "0 20px 50px rgba(0,0,0,0.8)",
            }}
          >
            <div
              style={{
                padding: "16px",
                background: "rgba(0,120,212,0.1)",
                borderBottom: `1px solid ${T.border}`,
                fontWeight: 600,
              }}
            >
              API 키 등록 확인
            </div>
            <div
              style={{
                padding: "20px",
                color: T.text,
                fontSize: "13px",
                lineHeight: 1.6,
              }}
            >
              <p style={{ margin: "0 0 10px" }}>API 키를 등록하시겠습니까?</p>
              {machineInfo.hostname && (
                <p style={{ margin: 0, color: T.textSub, fontSize: "12px" }}>
                  현재 기기{" "}
                  <strong style={{ color: T.white }}>
                    {machineInfo.hostname}
                  </strong>
                  이 자동으로 연동됩니다.
                </p>
              )}
            </div>
            <div style={{ padding: "16px", display: "flex", gap: "8px" }}>
              <button
                onClick={handleRegister}
                disabled={isSaving}
                style={{
                  flex: 1,
                  padding: "10px",
                  background: T.accent,
                  color: "#fff",
                  border: "none",
                  borderRadius: "6px",
                  cursor: "pointer",
                  fontWeight: 600,
                }}
              >
                {isSaving ? "등록 중..." : "예, 등록합니다"}
              </button>
              <button
                onClick={() => setShowConfirmModal(false)}
                style={{
                  flex: 1,
                  padding: "10px",
                  background: "transparent",
                  color: T.text,
                  border: `1px solid ${T.border}`,
                  borderRadius: "6px",
                  cursor: "pointer",
                }}
              >
                취소
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── 기기 삭제 확인 모달 ── */}
      {showDeleteModal && (
        <div
          style={{
            position: "absolute",
            inset: 0,
            background: "rgba(0,0,0,0.7)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            zIndex: 2000,
          }}
        >
          <div
            style={{
              width: "320px",
              background: T.panelBg,
              border: `1px solid ${T.danger}`,
              borderRadius: "10px",
              overflow: "hidden",
              boxShadow: "0 20px 50px rgba(0,0,0,0.8)",
            }}
          >
            <div
              style={{
                padding: "16px",
                background: "rgba(241,76,76,0.1)",
                borderBottom: `1px solid ${T.border}`,
                fontWeight: 600,
                color: T.danger,
              }}
            >
              기기 삭제 확인
            </div>
            <div
              style={{ padding: "24px", textAlign: "center", color: T.text }}
            >
              이 기기를 영구 삭제하시겠습니까?
            </div>
            <div style={{ padding: "16px", display: "flex", gap: "8px" }}>
              <button
                onClick={handleDeleteDevice}
                style={{
                  flex: 1,
                  padding: "10px",
                  background: T.danger,
                  color: "#fff",
                  border: "none",
                  borderRadius: "6px",
                  cursor: "pointer",
                }}
              >
                예, 삭제합니다
              </button>
              <button
                onClick={() => setShowDeleteModal(null)}
                style={{
                  flex: 1,
                  padding: "10px",
                  background: "transparent",
                  color: T.text,
                  border: `1px solid ${T.border}`,
                  borderRadius: "6px",
                  cursor: "pointer",
                }}
              >
                취소
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── 공용 알림 ── */}
      {notice.isOpen && (
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
          <div
            style={{
              width: "320px",
              background: T.panelBg,
              border: `1px solid ${notice.type === "success" ? T.success : notice.type === "error" ? T.danger : T.accent}`,
              borderRadius: "10px",
              overflow: "hidden",
              boxShadow: "0 20px 50px rgba(0,0,0,0.8)",
            }}
          >
            <div
              style={{
                padding: "16px",
                borderBottom: `1px solid ${T.border}`,
                fontWeight: 600,
                color:
                  notice.type === "success"
                    ? T.success
                    : notice.type === "error"
                      ? T.danger
                      : T.white,
              }}
            >
              {notice.title}
            </div>
            <div
              style={{ padding: "24px", textAlign: "center", color: T.text }}
            >
              {notice.message}
            </div>
            <div style={{ padding: "16px" }}>
              <button
                onClick={() => setNotice((p) => ({ ...p, isOpen: false }))}
                style={{
                  width: "100%",
                  padding: "10px",
                  background:
                    notice.type === "success"
                      ? T.success
                      : notice.type === "error"
                        ? T.danger
                        : T.accent,
                  color: "#fff",
                  border: "none",
                  borderRadius: "6px",
                  cursor: "pointer",
                  fontWeight: 600,
                }}
              >
                확인
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );

  function requestRegister() {
    if (!apiKey.trim()) {
      showAlert("입력 오류", "등록할 API Key를 입력해주세요.", "error");
      return;
    }
    setShowConfirmModal(true);
  }
}
