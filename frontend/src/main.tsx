/*
File : front/src/main.tsx
Author : 김지우
Create : 2026-03-31
Description : 메인 애플리케이션 진입점

Modification History :
- 2026-03-31 (김지우) : 초기 생성
*/

import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./index.css";

// Root 엘리먼트가 없을 경우를 대비한 타입 가드 처리
const rootElement = document.getElementById("root");

if (rootElement) {
  ReactDOM.createRoot(rootElement).render(<App />);
}
