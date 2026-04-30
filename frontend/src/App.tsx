import React from "react";
import SpecManagerModal from "./components/spec/SpecManagerModal";
import ApiKeyManagerModal from "./components/setting/ApiKeyManagerModal";
import ChatApp from "./components/ChatApp";
import { C } from "./constants/theme";

export default function App() {
  const isSpecView = window.location.search.includes("view=spec");
  const isApiView = window.location.search.includes("view=api");
  const apiRegistered = localStorage.getItem("skn23_api_key_registered") === "true";

  if (isApiView) return <div className="w-full h-screen" style={{ background: C.bg }}><ApiKeyManagerModal isOpen={true} onClose={() => window.history.back()} /></div>;

  if (isSpecView) {
    if (!apiRegistered) {
      return (
        <div className="w-full h-screen flex items-center justify-center" style={{ background: C.bg }}>
          <div className="text-center px-6 py-8 rounded-xl" style={{ background: C.card, border: `1px solid ${C.border}`, maxWidth: 320 }}>
            <p className="text-[14px] font-semibold mb-2" style={{ color: C.textPrimary }}>API 키 미등록</p>
            <p className="text-[12px] leading-relaxed" style={{ color: C.textSub }}>
              시방서 메뉴를 사용하려면 먼저 API 키를 등록해야 합니다.{"\n"}
              상단 메뉴 <strong>[Agent → API키 관리...]</strong>에서 등록 후 다시 시도하세요.
            </p>
          </div>
        </div>
      );
    }
    return <div className="w-full h-screen" style={{ background: C.bg }}><SpecManagerModal isOpen={true} onClose={() => window.history.back()} /></div>;
  }

  return <ChatApp />;
}
