/*
File : front/src/types.d.ts
Author : 김지우
Create : 2026-03-31
Description : 타입 정의

Modification History :
- 2026-03-31 (김지우) : 초기 생성
*/

interface Window {
  chrome?: {
    webview?: {
      postMessage: (message: any) => void;
      addEventListener: (type: string, listener: (event: any) => void) => void;
      removeEventListener: (
        type: string,
        listener: (event: any) => void,
      ) => void;
    };
  };
}
