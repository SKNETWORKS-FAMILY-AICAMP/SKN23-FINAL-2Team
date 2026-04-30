/*
File : front/src/types/cad.d.ts
Author : 김지우
Create : 2026-03-31
Description : 메인 애플리케이션 타입 정의

Modification History :
- 2026-03-31 (김지우) : 초기 생성
*/
declare global {
  interface Window {
    chrome?: {
      webview?: {
        postMessage: (message: any) => void;
        addEventListener: (
          type: string,
          listener: (event: MessageEvent) => void,
        ) => void;
        removeEventListener: (
          type: string,
          listener: (event: MessageEvent) => void,
        ) => void;
      };
    };
  }
}

export {};
