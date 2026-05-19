// TetrisLoadingGame.tsx
import { useCallback, useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";

type TetrisLoadingGameProps = {
  active: boolean;
  title?: string;
  subtitle?: string;
};

type Cell = number;
type Board = Cell[][];
type Piece = {
  matrix: number[][];
  x: number;
  y: number;
  type: number;
};

const COLS = 10;
const ROWS = 20;
const BLOCK = 22;
const BOARD_WIDTH = COLS * BLOCK;
const BOARD_HEIGHT = ROWS * BLOCK;

const PIECES: number[][][] = [
  [[1, 1, 1, 1]],

  [
    [1, 1],
    [1, 1],
  ],

  [
    [0, 1, 0],
    [1, 1, 1],
  ],

  [
    [1, 0, 0],
    [1, 1, 1],
  ],

  [
    [0, 0, 1],
    [1, 1, 1],
  ],

  [
    [0, 1, 1],
    [1, 1, 0],
  ],

  [
    [1, 1, 0],
    [0, 1, 1],
  ],
];

const createBoard = (): Board =>
  Array.from({ length: ROWS }, () => Array(COLS).fill(0));

const randomPiece = (): Piece => {
  const type = Math.floor(Math.random() * PIECES.length) + 1;
  const matrix = PIECES[type - 1].map((row) => [...row]);

  return {
    matrix,
    x: Math.floor(COLS / 2) - Math.ceil(matrix[0].length / 2),
    y: 0,
    type,
  };
};

const rotateMatrix = (matrix: number[][]) => {
  const rows = matrix.length;
  const cols = matrix[0].length;
  const rotated: number[][] = Array.from({ length: cols }, () =>
    Array(rows).fill(0)
  );

  for (let y = 0; y < rows; y += 1) {
    for (let x = 0; x < cols; x += 1) {
      rotated[x][rows - 1 - y] = matrix[y][x];
    }
  }

  return rotated;
};

export default function TetrisLoadingGame({
  active,
  title = "테트리스",
  subtitle = "기다리는 동안 테트리스를 플레이할 수 있습니다.",
}: TetrisLoadingGameProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const frameRef = useRef<number | null>(null);
  const lastTimeRef = useRef(0);
  const dropTimerRef = useRef(0);

  const boardRef = useRef<Board>(createBoard());
  const pieceRef = useRef<Piece>(randomPiece());
  const gameOverRef = useRef(false);

  const [score, setScore] = useState(0);
  const scoreRef = useRef(0);

  const [lines, setLines] = useState(0);
  const linesRef = useRef(0);

  const [gameOver, setGameOver] = useState(false);

  const resetGame = useCallback(() => {
    boardRef.current = createBoard();
    pieceRef.current = randomPiece();
    scoreRef.current = 0;
    linesRef.current = 0;
    dropTimerRef.current = 0;
    gameOverRef.current = false;

    setScore(0);
    setLines(0);
    setGameOver(false);
  }, []);

  const collides = (piece: Piece, board: Board) => {
    for (let y = 0; y < piece.matrix.length; y += 1) {
      for (let x = 0; x < piece.matrix[y].length; x += 1) {
        if (!piece.matrix[y][x]) continue;

        const nextX = piece.x + x;
        const nextY = piece.y + y;

        if (nextX < 0 || nextX >= COLS || nextY >= ROWS) {
          return true;
        }

        if (nextY >= 0 && board[nextY][nextX]) {
          return true;
        }
      }
    }

    return false;
  };

  const mergePiece = (piece: Piece, board: Board) => {
    const nextBoard = board.map((row) => [...row]);

    for (let y = 0; y < piece.matrix.length; y += 1) {
      for (let x = 0; x < piece.matrix[y].length; x += 1) {
        if (!piece.matrix[y][x]) continue;

        const boardX = piece.x + x;
        const boardY = piece.y + y;

        if (boardY >= 0 && boardY < ROWS && boardX >= 0 && boardX < COLS) {
          nextBoard[boardY][boardX] = piece.type;
        }
      }
    }

    return nextBoard;
  };

  const clearLines = (board: Board) => {
    const remaining = board.filter((row) => row.some((cell) => cell === 0));
    const cleared = ROWS - remaining.length;

    const newRows = Array.from({ length: cleared }, () => Array(COLS).fill(0));
    const nextBoard = [...newRows, ...remaining];

    if (cleared > 0) {
      linesRef.current += cleared;
      scoreRef.current += [0, 100, 300, 500, 800][cleared] ?? cleared * 200;

      setLines(linesRef.current);
      setScore(scoreRef.current);
    }

    return nextBoard;
  };

  const lockPiece = useCallback(() => {
    let nextBoard = mergePiece(pieceRef.current, boardRef.current);
    nextBoard = clearLines(nextBoard);

    const nextPiece = randomPiece();

    if (collides(nextPiece, nextBoard)) {
      gameOverRef.current = true;
      setGameOver(true);
      return;
    }

    boardRef.current = nextBoard;
    pieceRef.current = nextPiece;
  }, []);

  const movePiece = useCallback(
    (dx: number, dy: number) => {
      if (gameOverRef.current) return false;

      const nextPiece = {
        ...pieceRef.current,
        x: pieceRef.current.x + dx,
        y: pieceRef.current.y + dy,
      };

      if (!collides(nextPiece, boardRef.current)) {
        pieceRef.current = nextPiece;
        return true;
      }

      if (dy > 0) {
        lockPiece();
      }

      return false;
    },
    [lockPiece]
  );

  const rotatePiece = useCallback(() => {
    if (gameOverRef.current) return;

    const original = pieceRef.current;
    const rotated = {
      ...original,
      matrix: rotateMatrix(original.matrix),
    };

    const kicks = [0, -1, 1, -2, 2];

    for (const kick of kicks) {
      const candidate = {
        ...rotated,
        x: rotated.x + kick,
      };

      if (!collides(candidate, boardRef.current)) {
        pieceRef.current = candidate;
        return;
      }
    }
  }, []);

  const hardDrop = useCallback(() => {
    if (gameOverRef.current) {
      resetGame();
      return;
    }

    while (movePiece(0, 1)) {
      scoreRef.current += 2;
    }

    setScore(scoreRef.current);
  }, [movePiece, resetGame]);

  const drawCell = (
    ctx: CanvasRenderingContext2D,
    x: number,
    y: number,
    value: number,
    ghost = false
  ) => {
    if (!value) return;

    const px = x * BLOCK;
    const py = y * BLOCK;

    ctx.save();

    ctx.globalAlpha = ghost ? 0.28 : 1;
    ctx.fillStyle = [
      "",
      "#67e8f9",
      "#facc15",
      "#c084fc",
      "#60a5fa",
      "#fb923c",
      "#4ade80",
      "#f87171",
    ][value];

    ctx.fillRect(px + 1, py + 1, BLOCK - 2, BLOCK - 2);

    ctx.strokeStyle = "rgba(15, 23, 42, 0.65)";
    ctx.lineWidth = 2;
    ctx.strokeRect(px + 2, py + 2, BLOCK - 4, BLOCK - 4);

    ctx.restore();
  };

  const getGhostPiece = () => {
    let ghost = { ...pieceRef.current, matrix: pieceRef.current.matrix };

    while (!collides({ ...ghost, y: ghost.y + 1 }, boardRef.current)) {
      ghost = { ...ghost, y: ghost.y + 1 };
    }

    return ghost;
  };

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");

    if (!canvas || !ctx) return;

    ctx.clearRect(0, 0, BOARD_WIDTH, BOARD_HEIGHT);

    ctx.fillStyle = "#020617";
    ctx.fillRect(0, 0, BOARD_WIDTH, BOARD_HEIGHT);

    ctx.strokeStyle = "rgba(148, 163, 184, 0.12)";
    ctx.lineWidth = 1;

    for (let x = 0; x <= COLS; x += 1) {
      ctx.beginPath();
      ctx.moveTo(x * BLOCK, 0);
      ctx.lineTo(x * BLOCK, BOARD_HEIGHT);
      ctx.stroke();
    }

    for (let y = 0; y <= ROWS; y += 1) {
      ctx.beginPath();
      ctx.moveTo(0, y * BLOCK);
      ctx.lineTo(BOARD_WIDTH, y * BLOCK);
      ctx.stroke();
    }

    const board = boardRef.current;

    for (let y = 0; y < ROWS; y += 1) {
      for (let x = 0; x < COLS; x += 1) {
        drawCell(ctx, x, y, board[y][x]);
      }
    }

    const ghost = getGhostPiece();

    for (let y = 0; y < ghost.matrix.length; y += 1) {
      for (let x = 0; x < ghost.matrix[y].length; x += 1) {
        if (ghost.matrix[y][x]) {
          drawCell(ctx, ghost.x + x, ghost.y + y, ghost.type, true);
        }
      }
    }

    const piece = pieceRef.current;

    for (let y = 0; y < piece.matrix.length; y += 1) {
      for (let x = 0; x < piece.matrix[y].length; x += 1) {
        if (piece.matrix[y][x]) {
          drawCell(ctx, piece.x + x, piece.y + y, piece.type);
        }
      }
    }

    if (gameOverRef.current) {
      ctx.fillStyle = "rgba(2, 6, 23, 0.75)";
      ctx.fillRect(0, 0, BOARD_WIDTH, BOARD_HEIGHT);

      ctx.fillStyle = "#e5e7eb";
      ctx.font = "20px sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("GAME OVER", BOARD_WIDTH / 2, BOARD_HEIGHT / 2 - 10);

      ctx.font = "13px sans-serif";
      ctx.fillStyle = "#94a3b8";
      ctx.fillText("Space로 다시 시작", BOARD_WIDTH / 2, BOARD_HEIGHT / 2 + 18);
      ctx.textAlign = "start";
    }
  }, []);

  const update = useCallback(
    (delta: number) => {
      if (gameOverRef.current) return;

      const level = Math.floor(linesRef.current / 5);
      const dropInterval = Math.max(120, 700 - level * 70);

      dropTimerRef.current += delta * 1000;

      if (dropTimerRef.current >= dropInterval) {
        movePiece(0, 1);
        dropTimerRef.current = 0;
      }
    },
    [movePiece]
  );

  useEffect(() => {
    if (!active) {
      if (frameRef.current != null) {
        window.cancelAnimationFrame(frameRef.current);
        frameRef.current = null;
      }

      return;
    }

    wrapperRef.current?.focus();

    const tick = (time: number) => {
      if (!lastTimeRef.current) lastTimeRef.current = time;

      const delta = Math.min((time - lastTimeRef.current) / 1000, 0.033);
      lastTimeRef.current = time;

      update(delta);
      draw();

      frameRef.current = window.requestAnimationFrame(tick);
    };

    frameRef.current = window.requestAnimationFrame(tick);

    return () => {
      if (frameRef.current != null) {
        window.cancelAnimationFrame(frameRef.current);
        frameRef.current = null;
      }

      lastTimeRef.current = 0;
    };
  }, [active, draw, update]);

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!active) return;

    if (event.code === "ArrowLeft") {
      event.preventDefault();
      movePiece(-1, 0);
    }

    if (event.code === "ArrowRight") {
      event.preventDefault();
      movePiece(1, 0);
    }

    if (event.code === "ArrowDown") {
      event.preventDefault();
      movePiece(0, 1);
      scoreRef.current += 1;
      setScore(scoreRef.current);
    }

    if (event.code === "ArrowUp") {
      event.preventDefault();
      rotatePiece();
    }

    if (event.code === "Space") {
      event.preventDefault();
      hardDrop();
    }
  };

  if (!active) return null;

  return (
    <div
      ref={wrapperRef}
      tabIndex={0}
      onKeyDown={handleKeyDown}
      className="mx-auto mt-3 flex w-full flex-col items-center px-1 outline-none"
    >
      <div className="w-fit rounded-xl border border-slate-700/60 bg-slate-950/35 p-3 shadow-[0_0_28px_rgba(15,23,42,0.38)]">
        <div className="mb-2 flex items-center justify-between gap-4 text-[11px]">
          <span className="max-w-[120px] truncate font-medium text-slate-300" title={subtitle}>
            {title}
          </span>
          <span className="font-mono text-slate-400">
            SCORE {score} · LINE {lines}
          </span>
        </div>

        <canvas
          ref={canvasRef}
          width={BOARD_WIDTH}
          height={BOARD_HEIGHT}
          className="rounded-lg border border-slate-700/70 bg-slate-950"
          style={{
            width: BOARD_WIDTH,
            height: BOARD_HEIGHT,
            imageRendering: "pixelated",
          }}
          onClick={() => wrapperRef.current?.focus()}
        />

        <div className="mt-2 flex flex-wrap items-center justify-center gap-2 text-[11px] text-slate-500">
          <span>← → 이동</span>
          <span className="text-slate-700">|</span>
          <span>↑ 회전</span>
          <span className="text-slate-700">|</span>
          <span>Space 하드드롭</span>
          {gameOver && <span className="text-slate-400">다시 시작 가능</span>}
        </div>
      </div>
    </div>
  );
}
