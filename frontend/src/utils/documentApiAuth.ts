/** document API용 X-API-Key 헤더 (licenses 연동 org 검증) */

export const DEV_AUTH_BYPASS =
  import.meta.env.DEV || import.meta.env.VITE_DEV_AUTH_BYPASS === "true";

export function isAgentApiRegistered(): boolean {
  if (DEV_AUTH_BYPASS) return true;
  return localStorage.getItem("skn23_api_key_registered") === "true";
}

export function getDocumentApiHeaders(): Record<string, string> {
  const key = localStorage.getItem("skn23_api_key")?.trim() || "";
  if (!key) return {};
  return { "X-API-Key": key };
}

export function getAgentApiHeaders(): Record<string, string> {
  return getDocumentApiHeaders();
}
