/*
 * File        : frontend/src/api/chatApi.ts
 * Author      : 김다빈
 * Create      : 2026-04-06
 * Description : 대화기록 API 레이어
 *               - 백엔드 연결 시: fetch()로 FastAPI 호출
 *               - 백엔드 미연결 시: localStorage 폴백 (데모 모드)
 *
 * 백엔드 연동 시 TODO:
 *   1. USE_LOCAL_STORAGE = false 로 변경
 *   2. API_BASE를 실제 백엔드 URL로 설정
 */
import { getAgentApiHeaders } from "../utils/documentApiAuth";

// ── 설정 ─────────────────────────────────────────────────────────────
const USE_LOCAL_STORAGE = false; // true = 데모 모드 (localStorage), false = 백엔드 API
const API_BASE = ((import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") || "http://localhost:8000") + "/api/v1";
const LS_KEY = "cad-agent-sessions";

// 한글 에이전트명 → 도메인 코드 매핑
const AGENT_TO_DOMAIN: Record<string, string> = {
  전기: "elec",
  배관: "pipe",
  건축: "arch",
  소방: "fire",
  elec: "elec",
  pipe: "pipe",
  arch: "arch",
  fire: "fire",
};

// ── 타입 ─────────────────────────────────────────────────────────────

export interface SessionSummary {
  id: string;
  agent_type: string;
  title: string;
  dwg_filename: string;
  created_at: string; // ISO 8601
  message_count: number;
}

export interface ChatMessage {
  id: string;
  session_id: string;
  role: "user" | "agent";
  content: string;
  created_at: string;
}

export interface SessionDetail {
  session: SessionSummary;
  messages: ChatMessage[];
}

// ── localStorage 구조 ────────────────────────────────────────────────

interface LocalSession {
  id: string;
  agent_type: string;
  title: string;
  dwg_filename: string;
  created_at: string;
  messages: ChatMessage[];
}

function loadLocalSessions(): LocalSession[] {
  try {
    const raw = localStorage.getItem(LS_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

function saveLocalSessions(sessions: LocalSession[]) {
  localStorage.setItem(LS_KEY, JSON.stringify(sessions));
}

function generateId(): string {
  return (
    crypto.randomUUID?.() ??
    `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`
  );
}

// ── API 함수 ─────────────────────────────────────────────────────────

/** 에이전트별 세션 목록 조회 (device_id 기반 격리) */
export async function fetchSessionList(
  agentType: string,
): Promise<SessionSummary[]> {
  if (USE_LOCAL_STORAGE) {
    const all = loadLocalSessions();
    return all
      .filter((s) => s.agent_type === agentType)
      .sort(
        (a, b) =>
          new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
      )
      .map((s) => ({
        id: s.id,
        agent_type: s.agent_type,
        title: s.title,
        dwg_filename: s.dwg_filename,
        created_at: s.created_at,
        message_count: s.messages.length,
      }));
  }

  const domain = AGENT_TO_DOMAIN[agentType] ?? agentType;
  const deviceId = localStorage.getItem("skn23_device_id") ?? "";
  const params = new URLSearchParams({ agent_type: domain });
  if (deviceId) params.set("device_id", deviceId);

  const res = await fetch(`${API_BASE}/sessions?${params}`, {
    headers: { ...getAgentApiHeaders() },
  });
  if (!res.ok) throw new Error(`세션 목록 조회 실패: ${res.status}`);
  return res.json();
}

/** 특정 세션의 메시지 목록 조회 */
export async function fetchSessionMessages(
  sessionId: string,
): Promise<ChatMessage[]> {
  if (USE_LOCAL_STORAGE) {
    const all = loadLocalSessions();
    const session = all.find((s) => s.id === sessionId);
    return session?.messages ?? [];
  }

  const res = await fetch(`${API_BASE}/sessions/${sessionId}/messages`, {
    headers: { ...getAgentApiHeaders() },
  });
  if (!res.ok) {
    if (res.status === 404) return [];
    throw new Error(`메시지 조회 실패: ${res.status}`);
  }
  return res.json();
}

/** 새 세션 생성 */
export async function createSession(
  agentType: string,
  dwgFilename: string,
): Promise<SessionSummary> {
  const id = generateId();
  const now = new Date().toISOString();

  if (USE_LOCAL_STORAGE) {
    const session: LocalSession = {
      id,
      agent_type: agentType,
      title: "새 대화",
      dwg_filename: dwgFilename,
      created_at: now,
      messages: [],
    };
    const all = loadLocalSessions();
    all.unshift(session);
    saveLocalSessions(all);
    return {
      id: session.id,
      agent_type: session.agent_type,
      title: session.title,
      dwg_filename: session.dwg_filename,
      created_at: session.created_at,
      message_count: 0,
    };
  }

  const domain = AGENT_TO_DOMAIN[agentType] ?? agentType;
  const deviceId = localStorage.getItem("skn23_device_id") ?? "";
  const res = await fetch(`${API_BASE}/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...getAgentApiHeaders() },
    body: JSON.stringify({
      agent_type: domain,
      dwg_filename: dwgFilename,
      device_id: deviceId,
    }),
  });
  if (!res.ok) throw new Error(`세션 생성 실패: ${res.status}`);
  return res.json();
}

/** 세션에 메시지 추가 */
export async function addMessage(
  sessionId: string,
  role: "user" | "agent",
  content: string,
): Promise<ChatMessage> {
  const msg: ChatMessage = {
    id: generateId(),
    session_id: sessionId,
    role,
    content,
    created_at: new Date().toISOString(),
  };

  if (USE_LOCAL_STORAGE) {
    const all = loadLocalSessions();
    const session = all.find((s) => s.id === sessionId);
    if (session) {
      session.messages.push(msg);
      // 첫 사용자 메시지라면 세션 제목 업데이트
      if (role === "user" && session.title === "새 대화") {
        session.title =
          content.length > 30 ? content.slice(0, 30) + "..." : content;
      }
      saveLocalSessions(all);
    }
    return msg;
  }

  // 백엔드 모드: 메시지 저장은 WebSocket 핸들러(agent_service.chat)가 담당하므로
  // UI 상태용 객체만 반환 (중복 저장 방지)
  return msg;
}

/** 세션 삭제 (localStorage + 백엔드 DB 동시 삭제) */
export async function deleteSession(sessionId: string): Promise<void> {
  if (USE_LOCAL_STORAGE) {
    const all = loadLocalSessions();
    saveLocalSessions(all.filter((s) => s.id !== sessionId));
  }

  // 백엔드 DB에서도 삭제 시도 (세션이 없으면 404 무시)
  try {
    await fetch(`${API_BASE}/sessions/${sessionId}`, {
      method: "DELETE",
      headers: { ...getAgentApiHeaders() },
    });
  } catch {
    // 네트워크 오류는 무시 (로컬 삭제는 이미 완료)
  }
}
